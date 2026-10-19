"""Traffic attribution: strict payload parsing, first touch recorded once, campaign management."""

import asyncio

import pytest

from bot.services import build_services
from bot.services.attribution import MAX_PAYLOAD, parse_payload, start_link
from tests.factories import make_user

ADMIN = 9000001
U1, U2, U3 = 9101, 9102, 9103


@pytest.fixture
def svc(app_pool, clock):
    return build_services(app_pool, clock, clock, frozenset({ADMIN}))


@pytest.fixture
async def world(app_pool, svc):
    for uid in (U1, U2, U3):
        await make_user(app_pool, uid)
    res = await svc.attribution.create_campaign(ADMIN, "instagram", "Reel A")
    assert res.kind == "ok"
    return svc, res.campaign


async def acquisition(pool, uid):
    row = await pool.fetchrow("SELECT * FROM user_acquisition WHERE user_id = $1", uid)
    return dict(row) if row else None


# ------------------------------------------------------------------ the payload


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, ("none", None)),
        ("", ("none", None)),
        ("c_abcdefgh", ("code", "abcdefgh")),
        ("c_a2b3c4d5", ("code", "a2b3c4d5")),
        ("c_ABCDEFGH", ("bad", None)),  # codes are lowercase
        ("c_abcdefg", ("bad", None)),  # too short
        ("c_abcdefghi", ("bad", None)),  # too long
        ("c_abcdefg1", ("bad", None)),  # outside the alphabet (1, 0, 8, 9 are not used)
        ("x_abcdefgh", ("bad", None)),  # another namespace
        ("abcdefgh", ("bad", None)),
        ("c_abcdefgh ", ("bad", None)),
        (" c_abcdefgh", ("bad", None)),
        ("c_abcdefgh\n", ("bad", None)),
        ("c_abcd-fgh", ("bad", None)),
        ("c_abcdéfgh", ("bad", None)),
        ("c_абвгдежз", ("bad", None)),
        ("'; DROP TABLE users;--", ("bad", None)),
        ("c_abcdefgh' OR '1'='1", ("bad", None)),
        ("c_" + "a" * 5000, ("bad", None)),
        ("\x00c_abcdefgh", ("bad", None)),
    ],
)
def test_the_payload_is_parsed_strictly(raw, expected):
    assert parse_payload(raw) == expected


def test_an_oversized_payload_is_rejected_before_any_matching():
    assert parse_payload("c_" + "a" * (MAX_PAYLOAD)) == ("bad", None)
    assert parse_payload("a" * (MAX_PAYLOAD + 1)) == ("bad", None)


def test_the_link_is_built_from_the_bot_name_or_is_just_the_argument():
    assert start_link("my_bot", "abcdefgh") == "https://t.me/my_bot?start=c_abcdefgh"
    assert start_link(None, "abcdefgh") == "c_abcdefgh"
    assert len(start_link("x", "abcdefgh").split("start=")[1]) <= MAX_PAYLOAD


# ------------------------------------------------------------------ first touch


async def test_a_working_link_is_attributed_to_its_campaign_and_source(app_pool, world):
    svc, camp = world
    assert await svc.attribution.record_first_touch(U1, f"c_{camp['code']}") == "campaign"
    row = await acquisition(app_pool, U1)
    assert (row["kind"], row["campaign_id"], row["source"]) == ("campaign", camp["id"], "instagram")


async def test_no_link_is_direct(app_pool, world):
    svc, _ = world
    for payload in (None, ""):
        await app_pool.execute("DELETE FROM user_acquisition")
        assert await svc.attribution.record_first_touch(U1, payload) == "direct"
        row = await acquisition(app_pool, U1)
        assert (row["kind"], row["campaign_id"], row["source"]) == ("direct", None, None)


@pytest.mark.parametrize("payload", ["c_zzzzzzzz", "garbage", "c_" + "a" * 500, "'; DROP--"])
async def test_an_unknown_or_malformed_link_is_invalid_and_never_stored(app_pool, world, payload):
    svc, _ = world
    assert await svc.attribution.record_first_touch(U1, payload) == "invalid"
    row = await acquisition(app_pool, U1)
    assert (row["kind"], row["campaign_id"], row["source"]) == ("invalid", None, None)
    stored = await app_pool.fetchval("SELECT user_acquisition::text FROM user_acquisition")
    assert payload not in stored  # what the user sent is never kept


async def test_a_paused_campaign_is_recorded_as_invalid_not_as_acquisition(app_pool, world):
    svc, camp = world
    assert (await svc.attribution.set_status(ADMIN, camp["id"], paused=True)).kind == "ok"
    assert await svc.attribution.record_first_touch(U1, f"c_{camp['code']}") == "invalid"
    row = await acquisition(app_pool, U1)
    assert (row["kind"], row["source"]) == ("invalid", None)  # not attributed to the source
    assert row["campaign_id"] == camp["id"]  # remembered only so the page can show dead links
    assert (await svc.attribution.set_status(ADMIN, camp["id"], paused=False)).kind == "ok"
    assert await svc.attribution.record_first_touch(U2, f"c_{camp['code']}") == "campaign"


async def test_the_first_touch_is_never_replaced(app_pool, world):
    svc, camp = world
    other = (await svc.attribution.create_campaign(ADMIN, "youtube", "Video B")).campaign
    assert await svc.attribution.record_first_touch(U1, f"c_{camp['code']}") == "campaign"
    before = await acquisition(app_pool, U1)
    for payload in (f"c_{other['code']}", None, "garbage", f"c_{camp['code']}"):
        assert await svc.attribution.record_first_touch(U1, payload) == ""  # nothing stored
    assert await acquisition(app_pool, U1) == before
    assert await app_pool.fetchval("SELECT count(*) FROM user_acquisition") == 1


async def test_a_direct_first_touch_is_not_upgraded_by_a_later_link(app_pool, world):
    svc, camp = world
    await svc.attribution.record_first_touch(U1, None)
    assert await svc.attribution.record_first_touch(U1, f"c_{camp['code']}") == ""
    assert (await acquisition(app_pool, U1))["kind"] == "direct"


async def test_concurrent_starts_of_one_new_user_store_one_row(app_pool, world):
    svc, camp = world
    results = await asyncio.gather(
        *(svc.attribution.record_first_touch(U1, f"c_{camp['code']}") for _ in range(10))
    )
    assert sorted(results) == [""] * 9 + ["campaign"]
    assert await app_pool.fetchval("SELECT count(*) FROM user_acquisition") == 1


async def test_users_without_a_row_stay_without_one(app_pool, world):
    svc, camp = world
    await svc.attribution.record_first_touch(U1, f"c_{camp['code']}")
    assert await acquisition(app_pool, U2) is None  # nobody else is touched or invented


async def test_forgetting_an_account_removes_only_its_attribution(app_pool, world):
    svc, camp = world
    await svc.attribution.record_first_touch(U1, f"c_{camp['code']}")
    await svc.attribution.record_first_touch(U2, None)
    await svc.attribution.forget(U1)
    assert await acquisition(app_pool, U1) is None and await acquisition(app_pool, U2)
    assert await app_pool.fetchval("SELECT count(*) FROM campaigns") == 1


# ------------------------------------------------------------------ campaign management


async def test_creating_a_campaign_makes_a_random_code_and_an_audit_entry(app_pool, svc):
    res = await svc.attribution.create_campaign(ADMIN, " Instagram ", "  Reel A  ", is_test=True)
    c = res.campaign
    assert res.kind == "ok" and c["source"] == "instagram" and c["name"] == "Reel A"
    assert c["status"] == "active" and c["is_test"] is True and c["created_by"] == ADMIN
    assert len(c["code"]) == 8
    codes = {
        (await svc.attribution.create_campaign(ADMIN, "instagram", f"c{i}")).campaign["code"]
        for i in range(8)
    }
    assert len(codes) == 8 and c["code"] not in codes
    (audit,) = await app_pool.fetch(
        "SELECT * FROM admin_actions WHERE action = 'campaign_create' AND detail LIKE '%Reel A%'"
    )
    assert audit["admin_id"] == ADMIN


@pytest.mark.parametrize(
    "source", ["", "  ", "Ins ta", "1abc", "a", "x" * 33, "a;b", "a-b", "é", "'; drop", None]
)
async def test_a_bad_source_key_is_refused(app_pool, svc, source):
    assert (await svc.attribution.create_campaign(ADMIN, source, "Reel")).kind == "bad_source"
    assert await app_pool.fetchval("SELECT count(*) FROM campaigns") == 0


@pytest.mark.parametrize("name", ["", "   ", None, "\n\t"])
async def test_a_campaign_needs_a_name(app_pool, svc, name):
    assert (await svc.attribution.create_campaign(ADMIN, "instagram", name)).kind == "bad_name"


async def test_a_long_name_is_cut_not_stored_whole(app_pool, svc):
    res = await svc.attribution.create_campaign(ADMIN, "instagram", "n" * 500)
    assert res.kind == "ok" and len(res.campaign["name"]) <= 60


async def test_only_admins_manage_campaigns(app_pool, svc, world):
    _, camp = world
    assert (await svc.attribution.create_campaign(U1, "instagram", "x")).kind == "denied"
    assert (await svc.attribution.set_status(U1, camp["id"], paused=True)).kind == "denied"
    assert await svc.attribution.campaigns(U1) == [] and await svc.attribution.sources(U1) == []
    assert await app_pool.fetchval("SELECT count(*) FROM campaigns") == 1


async def test_pause_and_resume_are_audited_and_idempotent(app_pool, world):
    svc, camp = world
    assert (await svc.attribution.set_status(ADMIN, camp["id"], paused=True)).kind == "ok"
    assert (await svc.attribution.set_status(ADMIN, camp["id"], paused=True)).kind == "unchanged"
    assert (await svc.attribution.set_status(ADMIN, 999999, paused=True)).kind == "not_found"
    assert (await svc.attribution.set_status(ADMIN, camp["id"], paused=False)).kind == "ok"
    rows = await app_pool.fetch("SELECT detail FROM admin_actions WHERE action='campaign_status'")
    assert len(rows) == 2  # the no-op changed nothing and wrote nothing
    assert await app_pool.fetchval("SELECT count(*) FROM campaigns") == 1  # never deleted


async def test_sources_are_whatever_the_admin_created(app_pool, world):
    svc, _ = world
    await svc.attribution.create_campaign(ADMIN, "channel_a", "Post 1")
    assert await svc.attribution.sources(ADMIN) == ["channel_a", "instagram"]
