"""P3 admin service: authorization, statistics, ban / unban, reports queue, audit trail."""

import json

import asyncpg
import pytest

from bot.db import repo_chat, repo_safety
from bot.services import build_services
from bot.services.admin import MAX_USER_ID, REPORTS_PAGE, parse_user_id
from tests.factories import make_user, start_chat

ADMIN = 9000001
OTHER_ADMIN = 9000002


@pytest.fixture
def asvc(app_pool, clock):
    """Services whose ADMIN_IDS contain ADMIN and OTHER_ADMIN."""
    return build_services(app_pool, clock, clock, frozenset({ADMIN, OTHER_ADMIN}))


async def audit_rows(pool):
    return await pool.fetch("SELECT * FROM admin_actions ORDER BY id")


async def file_report(pool, reporter, reported, *, reason="spam", status="open", context=None):
    return await pool.fetchval(
        "INSERT INTO reports (reporter_id, reported_id, source, reason, status, context) "
        "VALUES ($1, $2, 'profile', $3, $4, $5::jsonb) RETURNING id",
        reporter,
        reported,
        reason,
        status,
        json.dumps(context) if context else None,
    )


# ---------------- input validation ----------------


@pytest.mark.parametrize(
    "raw,expected",
    [("123456789", 123456789), (" 42 ", 42), ("1", 1), ("0" * 3 + "7", 7)],
)
def test_parse_user_id_accepts_plain_ids(raw, expected):
    assert parse_user_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", " ", "0", "00", "-5", "+5", "1.5", "12 34", "abc", "1; DROP TABLE users", "٣٣٣", "１２３",
     "1" * 16, str(MAX_USER_ID), "0x10", "1e3", "\n"],
)  # fmt: skip
def test_parse_user_id_rejects_everything_else(raw):
    assert parse_user_id(raw) is None


# ---------------- authorization ----------------


async def test_non_admin_is_denied_everywhere_and_nothing_changes(app_pool, asvc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    rid = await file_report(app_pool, 1, 2)
    nobody = 777
    assert await asvc.admin.stats(nobody) is None
    assert await asvc.admin.user(nobody, 1) is None
    assert await asvc.admin.reports_page(nobody, 0) == ([], 0)
    assert await asvc.admin.report(nobody, rid) is None
    for res in (
        await asvc.admin.ban(nobody, 2),
        await asvc.admin.unban(nobody, 2),
        await asvc.admin.close_report(nobody, rid, dismiss=False),
    ):
        assert res.kind == "denied"
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = 2") == "active"
    assert await app_pool.fetchval("SELECT status FROM reports") == "open"
    assert await audit_rows(app_pool) == []


async def test_services_without_admin_ids_deny_everyone(app_pool, svc):
    await make_user(app_pool, 1)
    assert svc.admin.is_admin(ADMIN) is False
    assert (await svc.admin.ban(ADMIN, 1)).kind == "denied"


async def test_a_user_cannot_make_themselves_admin_by_any_input(app_pool, asvc):
    await make_user(app_pool, 5)
    assert (await asvc.admin.ban(5, 5)).kind == "denied"
    assert (await asvc.admin.unban(5, 5)).kind == "denied"
    assert await asvc.admin.stats(5) is None


# ---------------- statistics ----------------


async def test_statistics_are_aggregates_only(app_pool, asvc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await make_user(app_pool, 4, status="banned")
    await make_user(app_pool, 5, status="blocked_underage", with_profile=False)
    await make_user(app_pool, 6, status="onboarding", confirmed=False, with_profile=False)
    await start_chat(asvc, 1, 2)
    await asvc.matchmaking.search(3)  # queued
    await file_report(app_pool, 1, 3)
    await file_report(app_pool, 2, 3, status="dismissed")
    s = await asvc.admin.stats(ADMIN)
    assert dict(s) == {
        "total": 6,
        "active": 3,
        "onboarding": 1,
        "banned": 1,
        "underage": 1,
        "open_reports": 1,
        "active_chats": 1,
        "queued": 1,
        "matches": 0,
    }


# ---------------- user lookup ----------------


async def test_user_overview_shows_moderation_info_without_chat_content(app_pool, asvc):
    await make_user(app_pool, 1, city="Oslo")
    await make_user(app_pool, 2)
    await file_report(app_pool, 2, 1, context=[{"from": "reported", "text": "secret chat words"}])
    u = await asvc.admin.user(ADMIN, 1)
    assert (u["id"], u["status"], u["has_profile"], u["open_reports"]) == (1, "active", True, 1)
    assert u["age_confirmed_at"] is not None and u["city"] == "Oslo"
    assert "secret chat words" not in repr(dict(u))


async def test_unknown_or_out_of_range_user_is_none(asvc):
    assert await asvc.admin.user(ADMIN, 424242) is None
    assert await asvc.admin.user(ADMIN, 0) is None
    assert await asvc.admin.user(ADMIN, MAX_USER_ID) is None


# ---------------- ban ----------------


async def test_ban_ends_chat_leaves_queue_closes_reports_and_is_audited(app_pool, asvc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await start_chat(asvc, 1, 2)
    await file_report(app_pool, 3, 1)
    res = await asvc.admin.ban(ADMIN, 1)
    assert (res.kind, res.partner_id) == ("ok", 2)
    row = await app_pool.fetchrow("SELECT status, status_reason FROM users WHERE id = 1")
    assert (row["status"], row["status_reason"]) == ("banned", "admin_ban")
    assert await repo_chat.get_partner(app_pool, 2) is None
    assert await app_pool.fetchval("SELECT status FROM reports") == "actioned"
    (a,) = await audit_rows(app_pool)
    assert (a["admin_id"], a["action"], a["target_user_id"]) == (ADMIN, "ban", 1)


async def test_ban_removes_a_queued_user(app_pool, asvc):
    await make_user(app_pool, 1)
    await asvc.matchmaking.search(1)
    assert (await asvc.admin.ban(ADMIN, 1)).kind == "ok"
    assert await app_pool.fetchval("SELECT count(*) FROM chat_queue") == 0


async def test_ban_via_report_is_audited_with_the_report_id(app_pool, asvc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    rid = await file_report(app_pool, 1, 2)
    assert (await asvc.admin.ban(ADMIN, 2, report_id=rid)).kind == "ok"
    (a,) = await audit_rows(app_pool)
    assert (a["action"], a["report_id"], a["target_user_id"]) == ("report_ban", rid, 2)


async def test_banned_user_loses_random_chat_and_browse(app_pool, asvc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await asvc.admin.ban(ADMIN, 2)
    assert (await asvc.matchmaking.search(2)).kind == "not_eligible"
    assert (await asvc.browse.next_card(1)).card is None  # hidden from browse
    assert (await asvc.onboarding.start(2, None)).state == "blocked"


@pytest.mark.parametrize(
    "kw",
    [
        {"status": "blocked_underage", "with_profile": False},
        {"status": "deleted", "with_profile": False},
        {"status": "banned"},
    ],
)
async def test_only_onboarding_or_active_accounts_can_be_banned(app_pool, asvc, kw):
    await make_user(app_pool, 1, **kw)
    assert (await asvc.admin.ban(ADMIN, 1)).kind == "invalid_state"
    assert await audit_rows(app_pool) == []


async def test_admins_and_missing_users_cannot_be_banned(app_pool, asvc):
    await make_user(app_pool, OTHER_ADMIN)
    assert (await asvc.admin.ban(ADMIN, ADMIN)).kind == "protected"
    assert (await asvc.admin.ban(ADMIN, OTHER_ADMIN)).kind == "protected"
    assert (await asvc.admin.ban(ADMIN, 31337)).kind == "not_found"
    assert (await asvc.admin.ban(ADMIN, 0)).kind == "not_found"
    assert (await asvc.admin.ban(ADMIN, -4)).kind == "not_found"
    assert await app_pool.fetchval("SELECT status FROM users") == "active"
    assert await audit_rows(app_pool) == []


# ---------------- unban ----------------


async def test_unban_restores_an_adult_with_a_profile_to_active(app_pool, asvc):
    await make_user(app_pool, 1)
    await asvc.admin.ban(ADMIN, 1)
    res = await asvc.admin.unban(ADMIN, 1)
    assert (res.kind, res.detail) == ("ok", "active")
    row = await app_pool.fetchrow("SELECT status, status_reason FROM users WHERE id = 1")
    assert (row["status"], row["status_reason"]) == ("active", None)
    assert (await asvc.matchmaking.search(1)).kind == "queued"
    assert [a["action"] for a in await audit_rows(app_pool)] == ["ban", "unban"]


async def test_unban_without_a_profile_goes_back_to_onboarding(app_pool, asvc):
    await make_user(app_pool, 1, status="banned", with_profile=False, confirmed=False)
    res = await asvc.admin.unban(ADMIN, 1)
    assert (res.kind, res.detail) == ("ok", "onboarding")


async def test_unban_drops_a_banned_tombstone_so_start_works_again(app_pool, asvc):
    await make_user(app_pool, 1, status="banned")
    await app_pool.execute("INSERT INTO deletion_tombstones (user_id, reason) VALUES (1, 'banned')")
    assert (await asvc.onboarding.start(1, None)).state == "blocked"
    assert (await asvc.admin.unban(ADMIN, 1)).kind == "ok"
    assert (await asvc.onboarding.start(1, None)).state == "ready"


@pytest.mark.parametrize(
    "kw",
    [
        {"status": "blocked_underage", "with_profile": False},
        {"status": "deleted", "with_profile": False},
        {},  # active: nothing to unban
    ],
)
async def test_unban_never_lifts_underage_or_deleted_and_ignores_normal_users(app_pool, asvc, kw):
    await make_user(app_pool, 1, **kw)
    before = await app_pool.fetchval("SELECT status FROM users WHERE id = 1")
    assert (await asvc.admin.unban(ADMIN, 1)).kind == "invalid_state"
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = 1") == before
    assert await audit_rows(app_pool) == []


async def test_unban_unknown_user(asvc):
    assert (await asvc.admin.unban(ADMIN, 55555)).kind == "not_found"


# ---------------- reports ----------------


async def test_reports_page_lists_open_reports_newest_first_with_clamped_paging(app_pool, asvc):
    for uid in range(1, 9):
        await make_user(app_pool, uid)
    ids = [await file_report(app_pool, 1, uid) for uid in range(2, 8)]  # 6 open reports
    await file_report(app_pool, 1, 8, status="dismissed")
    rows, total = await asvc.admin.reports_page(ADMIN, 0)
    assert total == 6 and len(rows) == REPORTS_PAGE
    assert [r["id"] for r in rows] == sorted(ids, reverse=True)[:REPORTS_PAGE]
    rows2, _ = await asvc.admin.reports_page(ADMIN, 1)
    assert [r["id"] for r in rows2] == [ids[0]]
    assert [r["id"] for r in (await asvc.admin.reports_page(ADMIN, 99))[0]] == [ids[0]]
    assert (await asvc.admin.reports_page(ADMIN, -5))[0] == rows


async def test_report_view_has_references_reason_and_retained_context(app_pool, asvc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    ctx = [{"from": "reported", "text": "hello", "at": "2026-01-01T00:00:00+00:00"}]
    rid = await file_report(app_pool, 1, 2, reason="harassment", context=ctx)
    r = await asvc.admin.report(ADMIN, rid)
    assert (r["reporter_id"], r["reported_id"], r["reason"]) == (1, 2, "harassment")
    assert "hello" in r["context"]
    assert r["reported_status"] == "active" and r["open_against"] == 1


async def test_expired_context_is_not_shown(app_pool, asvc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    rid = await file_report(app_pool, 1, 2, context=[{"from": "reported", "text": "old"}])
    await app_pool.execute("UPDATE reports SET context_expires_at = now() - interval '1 day'")
    assert (await asvc.admin.report(ADMIN, rid))["context"] is None


async def test_resolve_and_dismiss_close_the_report_and_are_audited(app_pool, asvc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    r1 = await file_report(app_pool, 1, 2)
    r2 = await file_report(app_pool, 2, 1)
    assert (await asvc.admin.close_report(ADMIN, r1, dismiss=False)).kind == "ok"
    assert (await asvc.admin.close_report(ADMIN, r2, dismiss=True)).kind == "ok"
    st = {r["id"]: r["status"] for r in await app_pool.fetch("SELECT id, status FROM reports")}
    assert st == {r1: "actioned", r2: "dismissed"}
    acts = [(a["action"], a["report_id"], a["target_user_id"]) for a in await audit_rows(app_pool)]
    assert acts == [("report_resolve", r1, 2), ("report_dismiss", r2, 1)]
    # already handled: no second audit row, status unchanged
    assert (await asvc.admin.close_report(ADMIN, r1, dismiss=True)).kind == "already_handled"
    assert len(await audit_rows(app_pool)) == 2
    assert (await asvc.admin.close_report(ADMIN, 999999, dismiss=True)).kind == "not_found"


async def test_closing_a_report_does_not_ban_or_block_anyone(app_pool, asvc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    rid = await file_report(app_pool, 1, 2)
    await asvc.admin.close_report(ADMIN, rid, dismiss=True)
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = 2") == "active"
    assert not await repo_safety.is_blocked_either_way(app_pool, 1, 2)


# ---------------- audit + rate limit ----------------


async def test_audit_log_is_append_only(app_pool, asvc):
    await make_user(app_pool, 1)
    await asvc.admin.ban(ADMIN, 1)
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await app_pool.execute("UPDATE admin_actions SET action = 'unban'")
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await app_pool.execute("DELETE FROM admin_actions")
    assert len(await audit_rows(app_pool)) == 1


async def test_audit_rows_never_contain_chat_text_or_profile_data(app_pool, asvc):
    await make_user(app_pool, 1)
    await asvc.admin.ban(ADMIN, 1)
    (a,) = await audit_rows(app_pool)
    assert set(a.keys()) == {
        "id", "admin_id", "action", "target_user_id", "report_id", "detail", "created_at"
    }  # fmt: skip
    assert len(a["detail"]) <= 200


async def test_admin_actions_are_rate_limited(app_pool, asvc, clock):
    await make_user(app_pool, 1)
    kinds = [(await asvc.admin.unban(ADMIN, 1)).kind for _ in range(12)]
    assert kinds[:10] == ["invalid_state"] * 10 and kinds[10:] == ["rate_limited"] * 2
    clock.advance(30)
    assert (await asvc.admin.unban(ADMIN, 1)).kind == "invalid_state"  # bucket refilled


async def test_random_chat_and_browse_are_unaffected_for_normal_users(app_pool, asvc):
    await make_user(app_pool, 1, gender="m", looking="f")
    await make_user(app_pool, 2, gender="m", looking="f")
    assert (await asvc.matchmaking.search(1)).kind == "queued"
    assert (await asvc.matchmaking.search(2)).kind == "matched"
    assert (await asvc.browse.next_card(1)).card.name == "Name2"
