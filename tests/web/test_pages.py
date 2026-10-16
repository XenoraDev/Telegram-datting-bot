"""Users, reports, ban / unban, audit, statistics and privacy through the web panel."""

import asyncpg
import pytest

from bot.services import build_services
from tests.factories import make_user, start_chat
from tests.web.conftest import ADMIN, OTHER_ADMIN, file_report

# ---------------------------------------------------------------- users


async def seed(pool, n: int):
    for uid in range(1, n + 1):
        await make_user(pool, uid)


async def test_user_list_shows_the_required_columns(admin):
    await make_user(admin.pool, 5, gender="f", looking="m", age=31, city="Pune")
    await admin.pool.execute("UPDATE profiles SET country='IN' WHERE user_id=5")
    html = (await admin.client.get("/admin/users")).text
    for needle in ("Name5", ">31<", "Pune", ">f<", ">m<", ">IN<", "active", "Joined", "Last seen"):
        assert needle in html
    for col in ("Looking for", "Country", "City", "Chat", "Reports", "Restricted"):
        assert col in html


async def test_search_by_telegram_id(admin):
    await seed(admin.pool, 3)
    html = (await admin.client.get("/admin/users", params={"q": "2"})).text
    assert "Name2" in html and "Name1" not in html and "Name3" not in html


async def test_search_by_public_profile_id(admin):
    await seed(admin.pool, 3)
    pid = await admin.pool.fetchval("SELECT public_id FROM profiles WHERE user_id=3")
    for q in (pid.hex, str(pid)):  # with and without dashes
        html = (await admin.client.get("/admin/users", params={"q": q})).text
        assert "Name3" in html and "Name1" not in html


@pytest.mark.parametrize("q", ["abc", "1; DROP TABLE users", "' OR 1=1 --", "9" * 40, "0", "-5"])
async def test_garbage_search_returns_nothing_and_never_everything(admin, q):
    await seed(admin.pool, 3)
    r = await admin.client.get("/admin/users", params={"q": q})
    assert r.status_code == 200
    assert "Name1" not in r.text and "Name3" not in r.text
    assert await admin.pool.fetchval("SELECT count(*) FROM users") == 3


async def test_status_filter(admin):
    await make_user(admin.pool, 1)
    await make_user(admin.pool, 2, status="banned")
    html = (await admin.client.get("/admin/users", params={"status": "banned"})).text
    assert "Name2" in html and "Name1" not in html
    ignored = (await admin.client.get("/admin/users", params={"status": "bogus"})).text
    assert "Name1" in ignored and "Name2" in ignored


async def test_pagination(admin):
    await seed(admin.pool, 60)
    first = (await admin.client.get("/admin/users")).text
    assert "Page 1 of 3" in first and "60 total" in first and "Next" in first
    assert first.count("/admin/users/") == 25
    last = (await admin.client.get("/admin/users", params={"page": "3"})).text
    assert "Page 3 of 3" in last and "Next" not in last and last.count("/admin/users/") == 10
    clamped = (await admin.client.get("/admin/users", params={"page": "999"})).text
    assert "Page 3 of 3" in clamped
    for junk in ("-4", "x", "0", "99999999999"):
        assert (await admin.client.get("/admin/users", params={"page": junk})).status_code == 200


async def test_pager_links_keep_the_filters(admin):
    await seed(admin.pool, 30)
    html = (await admin.client.get("/admin/users", params={"status": "active"})).text
    assert "status=active" in html and "page=2" in html


async def test_user_detail_shows_safety_metadata(admin):
    await seed(admin.pool, 3)
    await file_report(admin.pool, 2, 1)
    await admin.pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (3, 1)")
    await admin.pool.execute(
        "INSERT INTO restrictions (user_id, kind, until, reason) "
        "VALUES (1, 'mute', now() + interval '1 hour', 'flood')"
    )
    html = (await admin.client.get("/admin/users/1")).text
    for needle in ("1 open", "Blocked by others", "mute until", "flood", "Name1", "Ban user"):
        assert needle in html


async def test_user_detail_for_missing_user_is_404(admin):
    assert (await admin.client.get("/admin/users/777")).status_code == 404
    assert (await admin.client.get("/admin/users/abc")).status_code == 404
    assert (await admin.client.get("/admin/users/0")).status_code == 404
    assert (await admin.client.get(f"/admin/users/{10**20}")).status_code == 404


async def test_user_content_is_html_escaped(admin):
    await make_user(admin.pool, 1)
    evil = "<script>alert(1)</script>"
    await admin.pool.execute("UPDATE profiles SET name=$1, city='<b>x</b>' WHERE user_id=1", evil)
    await admin.pool.execute(
        "INSERT INTO restrictions (user_id, kind, until, reason) "
        "VALUES (1, 'mute', now() + interval '1 hour', $1)",
        evil,
    )
    for path in ("/admin/users", "/admin/users/1"):
        html = (await admin.client.get(path)).text
        assert "<script>alert" not in html and "<script>alert" not in html
        assert "&lt;script&gt;" in html


async def test_search_term_is_escaped_when_echoed(admin):
    html = (await admin.client.get("/admin/users", params={"q": '"><script>x</script>'})).text
    assert "<script>x" not in html


# ---------------------------------------------------------------- ban / unban


async def test_ban_needs_a_confirmation_page_first(admin):
    await make_user(admin.pool, 1)
    page = await admin.client.get("/admin/users/1/ban")
    assert page.status_code == 200 and "Confirm ban" in page.text and 'name="note"' in page.text
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "active"


async def test_ban_ends_chat_leaves_queue_closes_reports_and_is_audited(admin):
    for uid in (1, 2, 3, 4):
        await make_user(admin.pool, uid)
    svc = build_services(admin.pool, admin.clock, admin.clock, admin.state.settings.admin_ids)
    await start_chat(svc, 1, 2)
    await svc.matchmaking.search(3)  # 3 waits in the queue
    await file_report(admin.pool, 4, 1)
    await file_report(admin.pool, 4, 3)

    r = await admin.post("/admin/users/1/ban", {"note": "harassment"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/users/1?msg=banned"
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "banned"
    assert not await admin.pool.fetchval("SELECT count(*) FROM chat_session_members WHERE active")
    assert await admin.pool.fetchval("SELECT status FROM reports WHERE reported_id=1") == "actioned"
    assert await admin.pool.fetchval("SELECT status FROM reports WHERE reported_id=3") == "open"

    r = await admin.post("/admin/users/3/ban")
    assert await admin.pool.fetchval("SELECT count(*) FROM chat_queue WHERE user_id=3") == 0

    rows = await admin.audit_rows()
    assert [(x["admin_id"], x["action"], x["target_user_id"]) for x in rows] == [
        (ADMIN, "ban", 1),
        (ADMIN, "ban", 3),
    ]
    assert "reason: harassment" in rows[0]["detail"]


async def test_ban_notifies_the_chat_partner_in_the_bot(admin):
    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(chat_id)

    for uid in (1, 2):
        await make_user(admin.pool, uid)
    svc = build_services(admin.pool, admin.clock, admin.clock, admin.state.settings.admin_ids)
    await start_chat(svc, 1, 2)
    admin.state.bot = Bot()
    await admin.post("/admin/users/1/ban")
    assert sent == [2]


async def test_admin_cannot_ban_themselves_or_another_admin(admin):
    await make_user(admin.pool, ADMIN)
    await make_user(admin.pool, OTHER_ADMIN)
    for uid in (ADMIN, OTHER_ADMIN):
        r = await admin.post(f"/admin/users/{uid}/ban")
        assert r.headers["location"].endswith("?msg=protected")
        assert await admin.pool.fetchval("SELECT status FROM users WHERE id=$1", uid) == "active"
    assert await admin.audit_rows() == []
    assert "cannot be banned" in (await admin.client.get(f"/admin/users/{OTHER_ADMIN}")).text


async def test_ban_of_unknown_user_is_reported_not_crashing(admin):
    r = await admin.post("/admin/users/424242/ban")
    assert r.status_code == 303 and r.headers["location"].endswith("?msg=missing")


async def test_underage_and_deleted_accounts_cannot_be_banned_or_unbanned(admin):
    await make_user(admin.pool, 1, status="blocked_underage")
    await make_user(admin.pool, 2, status="deleted", with_profile=False)
    for uid in (1, 2):
        for verb in ("ban", "unban"):
            r = await admin.post(f"/admin/users/{uid}/{verb}")
            assert r.headers["location"].endswith("?msg=state")
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "blocked_underage"
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=2") == "deleted"
    assert await admin.audit_rows() == []


async def test_unban_restores_a_banned_adult(admin):
    await make_user(admin.pool, 1)
    await admin.post("/admin/users/1/ban")
    r = await admin.post("/admin/users/1/unban")
    assert r.headers["location"] == "/admin/users/1?msg=unbanned"
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "active"
    assert [x["action"] for x in await admin.audit_rows()] == ["ban", "unban"]


async def test_unban_of_an_active_user_is_refused(admin):
    await make_user(admin.pool, 1)
    r = await admin.post("/admin/users/1/unban")
    assert r.headers["location"].endswith("?msg=state")


async def test_ban_reason_is_sanitised_and_bounded(admin):
    await make_user(admin.pool, 1)
    await admin.post("/admin/users/1/ban", {"note": "bad\x00\n\r\tx" + "y" * 500})
    detail = (await admin.audit_rows())[0]["detail"]
    assert len(detail) <= 200 and "\n" not in detail and "\x00" not in detail


async def test_bulk_bans_hit_the_action_rate_limit(admin):
    for uid in range(1, 16):
        await make_user(admin.pool, uid)
    kinds = []
    for uid in range(1, 16):
        r = await admin.post(f"/admin/users/{uid}/ban")
        kinds.append(r.headers["location"].split("msg=")[-1])
    assert "rate" in kinds and kinds[0] == "banned"


async def test_telegram_admin_and_web_share_the_same_rules(admin):
    """The web panel goes through the same service the bot's /admin uses."""
    await make_user(admin.pool, 1)
    svc = build_services(admin.pool, admin.clock, admin.clock, admin.state.settings.admin_ids)
    assert (await svc.admin.ban(ADMIN, 1)).kind == "ok"
    r = await admin.post("/admin/users/1/unban")
    assert r.headers["location"].endswith("?msg=unbanned")


# ---------------------------------------------------------------- reports


async def test_reports_newest_first_with_status_tabs_and_pagination(admin):
    await seed(admin.pool, 3)
    a = await file_report(admin.pool, 2, 1)
    b = await file_report(admin.pool, 3, 1, status="dismissed")
    c = await file_report(admin.pool, 3, 2, status="actioned")
    opened = (await admin.client.get("/admin/reports")).text
    assert f"#{a}" in opened and f"#{b}" not in opened and f"#{c}" not in opened
    everything = (await admin.client.get("/admin/reports", params={"status": "all"})).text
    assert everything.index(f"#{c}") < everything.index(f"#{b}") < everything.index(f"#{a}")
    assert "dismissed" in (await admin.client.get("/admin/reports?status=dismissed")).text
    assert f"#{c}" in (await admin.client.get("/admin/reports?status=actioned")).text
    for _ in range(30):
        await file_report(admin.pool, 2, 1, reason="other", status="dismissed")
    big = (await admin.client.get("/admin/reports?status=all")).text
    assert "Page 1 of 2" in big


async def test_report_detail_shows_fields_and_retained_context(admin):
    await seed(admin.pool, 2)
    rid = await file_report(
        admin.pool,
        2,
        1,
        reason="harassment",
        context=[{"from": "reported", "text": "hello <b>x</b>"}],
    )
    html = (await admin.client.get(f"/admin/reports/{rid}")).text
    for needle in ("harassment", "chat", ">2<", "Retained context", "hello &lt;b&gt;x&lt;/b&gt;"):
        assert needle in html
    assert "Resolve" in html and "Dismiss" in html and "Ban reported user" in html


async def test_expired_context_stays_hidden(admin):
    await seed(admin.pool, 2)
    rid = await file_report(admin.pool, 2, 1, context=[{"from": "reported", "text": "old secret"}])
    await admin.pool.execute(
        "UPDATE reports SET context_expires_at = now() - interval '1 second' WHERE id=$1", rid
    )
    html = (await admin.client.get(f"/admin/reports/{rid}")).text
    assert "old secret" not in html and "No chat context retained" in html
    await admin.pool.execute("UPDATE reports SET context = NULL WHERE id=$1", rid)
    assert "No chat context" in (await admin.client.get(f"/admin/reports/{rid}")).text


async def test_unknown_report_is_404(admin):
    assert (await admin.client.get("/admin/reports/99999")).status_code == 404


async def test_resolve_and_dismiss_are_audited(admin):
    await seed(admin.pool, 3)
    a = await file_report(admin.pool, 2, 1)
    b = await file_report(admin.pool, 3, 1)
    r = await admin.post(f"/admin/reports/{a}/resolve")
    assert r.headers["location"] == f"/admin/reports/{a}?msg=resolved"
    r = await admin.post(f"/admin/reports/{b}/dismiss")
    assert r.headers["location"].endswith("?msg=dismissed")
    statuses = {
        x["id"]: x["status"] for x in await admin.pool.fetch("SELECT id, status FROM reports")
    }
    assert statuses == {a: "actioned", b: "dismissed"}
    rows = await admin.audit_rows()
    assert [(x["action"], x["report_id"], x["target_user_id"]) for x in rows] == [
        ("report_resolve", a, 1),
        ("report_dismiss", b, 1),
    ]
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "active"


async def test_a_report_can_only_be_handled_once(admin):
    await seed(admin.pool, 2)
    rid = await file_report(admin.pool, 2, 1)
    await admin.post(f"/admin/reports/{rid}/resolve")
    r = await admin.post(f"/admin/reports/{rid}/dismiss")
    assert r.headers["location"].endswith("?msg=handled")
    assert len(await admin.audit_rows()) == 1


async def test_ban_from_a_report_links_the_report_in_the_audit_log(admin):
    await seed(admin.pool, 3)
    rid = await file_report(admin.pool, 2, 1)
    assert "Confirm ban" in (await admin.client.get(f"/admin/reports/{rid}/ban")).text
    r = await admin.post(f"/admin/reports/{rid}/ban", {"note": "clear abuse"})
    assert r.status_code == 303 and r.headers["location"].endswith("?msg=banned")
    assert await admin.pool.fetchval("SELECT status FROM users WHERE id=1") == "banned"
    assert await admin.pool.fetchval("SELECT status FROM reports WHERE id=$1", rid) == "actioned"
    row = (await admin.audit_rows())[0]
    assert (row["action"], row["report_id"], row["target_user_id"]) == ("report_ban", rid, 1)


async def test_report_ban_of_an_admin_is_refused(admin):
    await make_user(admin.pool, OTHER_ADMIN)
    await make_user(admin.pool, 2)
    rid = await file_report(admin.pool, 2, OTHER_ADMIN)
    r = await admin.post(f"/admin/reports/{rid}/ban")
    assert r.headers["location"].endswith("?msg=protected")
    assert (
        await admin.pool.fetchval("SELECT status FROM users WHERE id=$1", OTHER_ADMIN) == "active"
    )


# ---------------------------------------------------------------- audit


async def test_audit_log_lists_entries_newest_first_and_filters(admin):
    await seed(admin.pool, 4)
    await admin.post("/admin/users/1/ban")
    await admin.post("/admin/users/2/ban")
    await admin.post("/admin/users/1/unban")
    html = (await admin.client.get("/admin/audit")).text
    assert html.index("<td>unban</td>") < html.index("<td>ban</td>")
    assert "3 total" in html
    by_action = (await admin.client.get("/admin/audit", params={"action": "unban"})).text
    assert "1 total" in by_action
    by_user = (await admin.client.get("/admin/audit", params={"user": "2"})).text
    assert "1 total" in by_user
    by_admin = (await admin.client.get("/admin/audit", params={"admin": str(OTHER_ADMIN)})).text
    assert "No entries" in by_admin
    today = (await admin.pool.fetchval("SELECT now()::date")).isoformat()
    in_range = await admin.client.get("/admin/audit", params={"from": today, "to": today})
    assert "3 total" in in_range.text
    old = await admin.client.get("/admin/audit", params={"from": "2001-01-01", "to": "2001-01-02"})
    assert "No entries" in old.text


async def test_audit_filter_input_is_validated(admin):
    for params in (
        {"action": "'; DROP TABLE x"},
        {"user": "abc"},
        {"from": "yesterday"},
        {"admin": "-1"},
    ):
        assert (await admin.client.get("/admin/audit", params=params)).status_code == 200


async def test_audit_log_has_no_write_endpoints_and_the_database_refuses_changes(admin):
    await make_user(admin.pool, 1)
    await admin.post("/admin/users/1/ban")
    for method in ("post", "put", "patch", "delete"):
        r = await getattr(admin.client, method)("/admin/audit")
        assert r.status_code in (403, 405)
    r = await admin.post("/admin/audit/1/delete")
    assert r.status_code in (404, 405)
    with pytest.raises(asyncpg.RaiseError):
        await admin.pool.execute("DELETE FROM admin_actions")
    with pytest.raises(asyncpg.RaiseError):
        await admin.pool.execute("UPDATE admin_actions SET detail='x'")
    assert len(await admin.audit_rows()) == 1


# ---------------------------------------------------------------- statistics


async def test_dashboard_and_statistics_match_the_database(admin):
    for uid, status in (
        (1, "active"),
        (2, "active"),
        (3, "onboarding"),
        (4, "banned"),
        (5, "blocked_underage"),
    ):
        await make_user(admin.pool, uid, status=status)
    for uid in (6, 7, 8):
        await make_user(admin.pool, uid)
    svc = build_services(admin.pool, admin.clock, admin.clock)
    await start_chat(svc, 1, 2)
    await svc.matchmaking.search(6)
    await file_report(admin.pool, 7, 8)
    await file_report(admin.pool, 8, 7, status="dismissed")
    await admin.pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES (7, 8)")

    expected = {
        "Total users": 8,
        "Active users": 5,
        "Onboarding": 1,
        "Banned": 1,
        "Underage-blocked": 1,
        "Open reports": 1,
        "Active chats": 1,
        "Queued users": 1,
        "Matches": 1,
    }
    for path in ("/admin/", "/admin/statistics"):
        html = (await admin.client.get(path)).text
        for label, value in expected.items():
            assert f"<span>{label}</span><strong>{value}</strong>" in html, (path, label)
    detail = (await admin.client.get("/admin/statistics")).text
    assert "<span>Dismissed</span><strong>1</strong>" in detail


# ---------------------------------------------------------------- privacy


async def test_there_is_no_route_that_serves_chat_content(admin):
    paths = {getattr(r, "path", "") for r in admin.app.routes}
    for route in admin.app.routes:  # nested routers
        paths |= {getattr(sub, "path", "") for sub in getattr(route, "routes", [])}
    joined = " ".join(paths).lower()
    for word in ("chat", "message", "transcript", "history", "conversation"):
        assert word not in joined
    for path in ("/admin/chats", "/admin/chat/1", "/admin/messages", "/admin/transcripts"):
        assert (await admin.client.get(path)).status_code == 404


async def test_live_chat_text_never_reaches_the_panel(admin):
    for uid in (1, 2):
        await make_user(admin.pool, uid)
    svc = build_services(admin.pool, admin.clock, admin.clock, admin.state.settings.admin_ids)
    sid = await start_chat(svc, 1, 2)
    svc.buffer.add(sid, 1, "the-live-secret-words")  # only the RAM report buffer holds this
    for path in (
        "/admin/",
        "/admin/users",
        "/admin/users/1",
        "/admin/users/2",
        "/admin/audit",
        "/admin/statistics",
    ):
        assert "the-live-secret-words" not in (await admin.client.get(path)).text


async def test_no_contact_information_is_rendered(admin):
    await make_user(admin.pool, 1)
    await admin.pool.execute("UPDATE users SET username='private_handle' WHERE id=1;")
    await admin.pool.execute(
        "UPDATE profiles SET bio='call +91 98765 43210 or mail me@secret.example' WHERE user_id=1"
    )
    for path in ("/admin/users", "/admin/users/1"):
        html = (await admin.client.get(path)).text
        for secret in ("98765", "secret.example", "private_handle", "@"):
            assert secret not in html, (path, secret)


async def test_no_secrets_appear_in_any_page(admin):
    await make_user(admin.pool, 1)
    for path in (
        "/login",
        "/admin/",
        "/admin/users",
        "/admin/users/1",
        "/admin/reports",
        "/admin/audit",
    ):
        html = (await admin.client.get(path)).text
        assert "TEST-TOKEN" not in html and "s" * 20 not in html
        assert admin.sid not in html


async def test_csrf_token_is_in_forms_but_is_not_the_session_id(admin):
    html = (await admin.client.get("/admin/")).text
    assert admin.token() in html and admin.sid not in html
