"""Traffic page: aggregates only, campaign management, CSV, and who may do what."""

import csv
import io
import re

import pytest

from tests.factories import make_user
from tests.web.conftest import ADMIN


@pytest.fixture
async def desk(admin):
    admin.attr = admin.state.services.attribution
    return admin


async def make(desk, source="instagram", name="Reel A", **kw):
    res = await desk.attr.create_campaign(ADMIN, source, name, **kw)
    assert res.kind == "ok"
    return res.campaign


async def acquire(desk, uid, campaign=None, kind=None):
    await make_user(desk.pool, uid)
    kind = kind or ("campaign" if campaign else "direct")
    await desk.pool.execute(
        "INSERT INTO user_acquisition (user_id, kind, campaign_id, source) VALUES ($1,$2,$3,$4)",
        uid,
        kind,
        campaign["id"] if campaign else None,
        campaign["source"] if campaign and kind == "campaign" else None,
    )


async def campaigns_count(desk):
    return await desk.pool.fetchval("SELECT count(*) FROM campaigns")


# ------------------------------------------------------------------ the page


async def test_the_traffic_page_is_in_the_navigation_and_shows_an_empty_state(desk):
    home = (await desk.client.get("/admin/")).text
    assert 'href="/admin/traffic"' in home and ">Traffic<" in home
    page = (await desk.client.get("/admin/traffic")).text
    assert "No new accounts in this period" in page and "Create a campaign" in page


async def test_the_page_shows_the_numbers_per_source_and_campaign(desk):
    a = await make(desk, "instagram", "Reel A")
    b = await make(desk, "youtube", "Video B")
    for uid in (9501, 9502, 9503):
        await acquire(desk, uid, a)
    await acquire(desk, 9504, b)
    await acquire(desk, 9505)  # direct
    await acquire(desk, 9506, a, kind="invalid")
    page = (await desk.client.get("/admin/traffic")).text
    assert "Reel A" in page and "Video B" in page and "<code>instagram</code>" in page
    tiles = dict(re.findall(r"<span>([^<]+)</span><strong>([^<]*)</strong>", page))
    assert tiles["New accounts"] == "6" and tiles["From a campaign"] == "4"
    assert tiles["Direct (no link)"] == "1" and tiles["Link not usable"] == "1"
    assert "Funnel (all new accounts in the period)" in page


async def test_test_campaigns_are_hidden_until_asked(desk):
    t = await make(desk, "instagram", "Secret test", is_test=True)
    await acquire(desk, 9501, t)
    assert "Secret test" not in (await desk.client.get("/admin/traffic")).text
    assert "Secret test" in (await desk.client.get("/admin/traffic?tests=1")).text


@pytest.mark.parametrize(
    "query",
    [
        "days=7",
        "days=90",
        "days=abc",
        "days=-1",
        "days=99999999",
        "from=2026-01-01&to=2026-01-31",
        "from=2026-02-01&to=2026-01-01",  # backwards
        "from=1900-01-01&to=2999-01-01",  # absurdly wide
        "from=junk&to=junk",
        "from=%27%3B--&to=%3Cscript%3E",
        "tests=maybe",
    ],
)
async def test_any_period_input_is_handled(desk, query):
    r = await desk.client.get(f"/admin/traffic?{query}")
    assert r.status_code == 200 and "<script>" not in r.text
    assert (await desk.client.get(f"/admin/traffic/export.csv?{query}")).status_code == 200


async def test_a_custom_period_only_counts_accounts_acquired_in_it(desk):
    a = await make(desk)
    await acquire(desk, 9501, a)
    await desk.pool.execute("DELETE FROM user_acquisition")  # (rows are immutable; re-insert dated)
    await make_user(desk.pool, 9502)
    await desk.pool.execute(
        "INSERT INTO user_acquisition (user_id, kind, campaign_id, source, acquired_at) "
        "VALUES (9502, 'campaign', $1, 'instagram', '2026-01-15T10:00:00+00')",
        a["id"],
    )
    inside = (await desk.client.get("/admin/traffic?from=2026-01-15&to=2026-01-15")).text
    outside = (await desk.client.get("/admin/traffic?from=2026-01-16&to=2026-01-20")).text
    assert "No new accounts" not in inside and "No new accounts" in outside


async def test_the_page_shows_no_user_identifying_data(desk):
    a = await make(desk)
    for uid in (7771001, 7771002):
        await acquire(desk, uid, a)
    await desk.pool.execute("UPDATE users SET username = 'secret_person' WHERE id = 7771001")
    for path in ("/admin/traffic", "/admin/traffic?tests=1", "/admin/traffic/export.csv"):
        text = (await desk.client.get(path)).text
        for leak in ("7771001", "7771002", "secret_person"):
            assert leak not in text


async def test_names_are_escaped_in_the_page(desk):
    await make(desk, "instagram", "<script>alert(1)</script>")
    page = (await desk.client.get("/admin/traffic")).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page


async def test_a_get_never_changes_anything(desk):
    a = await make(desk)
    before = await desk.pool.fetchrow("SELECT * FROM campaigns WHERE id = $1", a["id"])
    for path in ("/admin/traffic", "/admin/traffic/export.csv", "/admin/traffic?days=7"):
        await desk.client.get(path)
    assert await desk.pool.fetchrow("SELECT * FROM campaigns WHERE id = $1", a["id"]) == before
    assert await campaigns_count(desk) == 1


# ------------------------------------------------------------------ links and campaigns


async def test_creating_a_campaign_shows_a_link_built_from_the_bot_name(desk):
    r = await desk.post(
        "/admin/traffic/campaigns", {"source": "Instagram", "name": " Reel A ", "test": ""}
    )
    assert r.status_code == 303 and r.headers["location"].endswith("msg=camp_created")
    (c,) = await desk.pool.fetch("SELECT * FROM campaigns")
    assert (c["source"], c["name"], c["status"], c["created_by"]) == (
        "instagram",
        "Reel A",
        "active",
        ADMIN,
    )
    page = (await desk.client.get("/admin/traffic")).text
    assert f"https://t.me/my_test_bot?start=c_{c['code']}" in page
    (audit,) = await desk.pool.fetch("SELECT * FROM admin_actions WHERE action='campaign_create'")
    assert audit["admin_id"] == ADMIN


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"source": "", "name": "x"}, "camp_bad_source"),
        ({"source": "Bad Source!", "name": "x"}, "camp_bad_source"),
        ({"source": "a" * 40, "name": "x"}, "camp_bad_source"),
        ({"source": "'; drop table users;--", "name": "x"}, "camp_bad_source"),
        ({"source": "instagram", "name": ""}, "camp_bad_name"),
        ({"source": "instagram", "name": "   "}, "camp_bad_name"),
    ],
)
async def test_bad_campaign_input_creates_nothing(desk, data, msg):
    r = await desk.post("/admin/traffic/campaigns", data)
    assert r.status_code == 303 and msg in r.headers["location"]
    assert await campaigns_count(desk) == 0


async def test_pause_and_resume_a_campaign(desk):
    a = await make(desk)
    r = await desk.post(f"/admin/traffic/campaigns/{a['id']}/status", {"paused": "1"})
    assert "camp_paused" in r.headers["location"]
    assert await desk.pool.fetchval("SELECT status FROM campaigns") == "paused"
    again = await desk.post(f"/admin/traffic/campaigns/{a['id']}/status", {"paused": "1"})
    assert "camp_unchanged" in again.headers["location"]
    r = await desk.post(f"/admin/traffic/campaigns/{a['id']}/status", {"paused": "0"})
    assert "camp_resumed" in r.headers["location"]
    missing = await desk.post("/admin/traffic/campaigns/999999/status", {"paused": "1"})
    assert "missing" in missing.headers["location"]
    assert await campaigns_count(desk) == 1  # never deleted


async def test_posts_need_csrf_and_our_own_origin(desk):
    a = await make(desk)
    for path, data in (
        ("/admin/traffic/campaigns", {"source": "youtube", "name": "x"}),
        (f"/admin/traffic/campaigns/{a['id']}/status", {"paused": "1"}),
    ):
        assert (await desk.post(path, data, with_csrf=False)).status_code == 403
        r = await desk.post(path, data, headers={"origin": "https://evil.example"})
        assert r.status_code == 403
    assert await campaigns_count(desk) == 1
    assert await desk.pool.fetchval("SELECT status FROM campaigns") == "active"


async def test_everything_needs_an_admin_session(desk, web):
    a = await make(desk)
    web.client.cookies.clear()
    for path in ("/admin/traffic", "/admin/traffic/export.csv"):
        r = await web.client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in ("/admin/traffic/campaigns", f"/admin/traffic/campaigns/{a['id']}/status"):
        r = await web.client.post(path, data={"source": "x", "name": "y", "csrf_token": "t"})
        assert r.status_code == 403
    assert await campaigns_count(desk) == 1


# ------------------------------------------------------------------ the CSV


async def test_the_csv_holds_the_aggregates_and_nothing_personal(desk):
    a = await make(desk, "instagram", "Reel A")
    for uid in (9501, 9502):
        await acquire(desk, uid, a)
    r = await desk.client.get("/admin/traffic/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith('attachment; filename="traffic-')
    assert r.headers["cache-control"] == "no-store"
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0][:5] == ["level", "source", "campaign", "status", "acquired"]
    by_level = {(row[0], row[1], row[2]): row for row in rows[1:]}
    assert by_level[("total", "", "")][4] == "2"
    assert by_level[("source", "instagram", "")][4] == "2"
    assert by_level[("campaign", "instagram", "Reel A")][4] == "2"
    assert "9501" not in r.text and "9502" not in r.text


async def test_csv_cells_that_look_like_formulas_are_defused(desk):
    await make(desk, "instagram", '=HYPERLINK("http://evil","x")')
    await make(desk, "instagram", "@SUM(1+1)")
    await make(desk, "instagram", "+1+1")
    await make(desk, "instagram", "-2+3")
    text = (await desk.client.get("/admin/traffic/export.csv")).text
    names = [row[2] for row in csv.reader(io.StringIO(text)) if row[0] == "campaign"]
    assert names and all(n.startswith("'") for n in names if n[1:2] in "=@+-" or n[:1] == "'")
    assert not any(n[:1] in "=@+-" for n in names)
