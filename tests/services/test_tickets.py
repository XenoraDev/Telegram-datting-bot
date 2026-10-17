"""Support tickets: opening, the 3-open cap, follow-ups, closing, ownership, admin, privacy."""

import asyncio

import pytest

from bot.services import build_services
from bot.services.chat_relay import Incoming
from tests.factories import make_user, start_chat
from tests.services.test_privacy import dump_all_tables

ADMIN = 9000001
ADMINS = frozenset({ADMIN, 9000002})
MARK = "zq7-support-marker-xk3"


@pytest.fixture
def tsvc(app_pool, clock):
    return build_services(app_pool, clock, clock, ADMINS)


@pytest.fixture
async def users(app_pool):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)


async def open_ticket(tsvc, uid=1, category="payment", text="my payment is missing"):
    res = await tsvc.tickets.open(uid, category, text)
    assert res.kind == "ok", res
    return res.ticket_id


# ------------------------------------------------------------------ opening


async def test_opening_a_ticket_stores_the_category_and_the_first_message(app_pool, tsvc, users):
    tid = await open_ticket(tsvc, text="  hello\n\n  there  ")
    t = await app_pool.fetchrow("SELECT * FROM support_tickets WHERE id = $1", tid)
    assert (t["user_id"], t["category"], t["status"]) == (1, "payment", "open")
    (m,) = await app_pool.fetch("SELECT * FROM support_messages")
    assert (m["sender"], m["admin_id"], m["ticket_text"]) == ("user", None, "hello\n\nthere")


@pytest.mark.parametrize(
    ("category", "text", "kind"),
    [
        ("refunds", "x", "bad_category"),
        ("payment", "", "empty"),
        ("payment", "   \n  ", "empty"),
        ("payment", None, "empty"),
        ("payment", "x" * 2001, "too_long"),
    ],
)
async def test_junk_is_refused_and_nothing_is_stored(app_pool, tsvc, users, category, text, kind):
    assert (await tsvc.tickets.open(1, category, text)).kind == kind
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 0


async def test_exactly_2000_characters_are_fine(tsvc, users):
    assert (await tsvc.tickets.open(1, "other", "y" * 2000)).kind == "ok"


async def test_only_an_active_account_can_open_a_ticket(app_pool, tsvc):
    await make_user(app_pool, 5, status="banned")
    await make_user(app_pool, 6, status="onboarding", confirmed=False)
    for uid in (5, 6, 404):
        assert (await tsvc.tickets.open(uid, "other", "help")).kind == "not_active"


async def test_at_most_three_unfinished_tickets_and_closing_frees_a_slot(tsvc, clock, users):
    ids = []
    for _ in range(3):
        ids.append(await open_ticket(tsvc))
        clock.advance(61)
    assert (await tsvc.tickets.open(1, "other", "a fourth")).kind == "too_many"
    assert (await tsvc.tickets.open(2, "other", "someone else is unaffected")).kind == "ok"
    assert (await tsvc.tickets.close_by_user(1, ids[0])).kind == "ok"
    assert (await tsvc.tickets.open(1, "other", "now there is room")).kind == "ok"
    # an answered ticket is still unfinished
    await tsvc.tickets.reply(ADMIN, ids[1], "answer")
    clock.advance(61)
    assert (await tsvc.tickets.open(1, "other", "no room again")).kind == "too_many"


async def test_opening_is_rate_limited(tsvc, users):
    kinds = [(await tsvc.tickets.open(1, "other", f"t{i}")).kind for i in range(5)]
    assert kinds[:3] == ["ok"] * 3 and "rate_limited" in kinds[3:]


async def test_simultaneous_opens_cannot_exceed_the_cap(app_pool, tsvc, users):
    # the limiter is per call; use several users' worth of bursts on one user via the DB path
    results = await asyncio.gather(
        *[tsvc.tickets.open(1, "other", f"t{i}") for i in range(3)],
        *[app_pool.execute("INSERT INTO support_tickets (user_id, category) VALUES (1, 'other')")
          for _ in range(5)],
        return_exceptions=True,
    )  # fmt: skip
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") <= 3
    assert any(not isinstance(r, Exception) for r in results)


# ------------------------------------------------------------------ follow-ups and closing


async def test_a_follow_up_reopens_an_answered_ticket(app_pool, tsvc, users):
    tid = await open_ticket(tsvc)
    await tsvc.tickets.reply(ADMIN, tid, "did you pay?")
    assert await app_pool.fetchval("SELECT status FROM support_tickets") == "answered"
    assert (await tsvc.tickets.user_reply(1, tid, "yes, UTR is above")).kind == "ok"
    assert await app_pool.fetchval("SELECT status FROM support_tickets") == "open"
    senders = [
        r["sender"] for r in await app_pool.fetch("SELECT sender FROM support_messages ORDER BY id")
    ]
    assert senders == ["user", "admin", "user"]


async def test_nobody_can_use_somebody_elses_ticket(app_pool, tsvc, users):
    tid = await open_ticket(tsvc, uid=1)
    assert (await tsvc.tickets.user_reply(2, tid, "hijack")).kind == "not_found"
    assert (await tsvc.tickets.close_by_user(2, tid)).kind == "not_found"
    assert await tsvc.tickets.my_ticket(2, tid) is None
    assert await tsvc.tickets.my_tickets(2) == []
    assert [r["id"] for r in await tsvc.tickets.my_tickets(1)] == [tid]
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 1
    assert await app_pool.fetchval("SELECT status FROM support_tickets") == "open"


async def test_a_closed_ticket_takes_no_more_messages(tsvc, users):
    tid = await open_ticket(tsvc)
    assert (await tsvc.tickets.close_by_user(1, tid)).kind == "ok"
    assert (await tsvc.tickets.close_by_user(1, tid)).kind == "closed"
    assert (await tsvc.tickets.user_reply(1, tid, "more")).kind == "closed"
    assert (await tsvc.tickets.reply(ADMIN, tid, "more")).kind == "closed"


async def test_a_ticket_cannot_grow_without_bound(app_pool, tsvc, clock, users):
    tid = await open_ticket(tsvc)
    for i in range(39):
        clock.advance(11)
        assert (await tsvc.tickets.user_reply(1, tid, f"m{i}")).kind == "ok"
    clock.advance(11)
    assert (await tsvc.tickets.user_reply(1, tid, "one too many")).kind == "full"
    assert (await tsvc.tickets.reply(ADMIN, tid, "also refused")).kind == "full"
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 40


async def test_the_thread_returned_to_the_user_is_the_newest_messages_oldest_first(
    tsvc, clock, users
):
    tid = await open_ticket(tsvc, text="m0")
    for i in range(1, 9):
        clock.advance(11)
        await tsvc.tickets.user_reply(1, tid, f"m{i}")
    view = await tsvc.tickets.my_ticket(1, tid)
    assert [m["ticket_text"] for m in view["messages"]] == [f"m{i}" for i in range(3, 9)]


# ------------------------------------------------------------------ admin


async def test_a_non_admin_can_do_nothing_in_the_admin_api(app_pool, tsvc, users):
    tid = await open_ticket(tsvc)
    assert (await tsvc.tickets.list_tickets(1, status=None, page=0)).rows == []
    assert await tsvc.tickets.detail(1, tid) is None
    assert await tsvc.tickets.counts(1) is None
    assert (await tsvc.tickets.reply(1, tid, "I am not an admin")).kind == "denied"
    assert (await tsvc.tickets.close(1, tid)).kind == "denied"
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 1
    assert await app_pool.fetchval("SELECT count(*) FROM admin_actions") == 0


async def test_listing_filters_by_status_and_counts(tsvc, clock, users):
    a = await open_ticket(tsvc, uid=1)
    b = await open_ticket(tsvc, uid=2)
    c = await open_ticket(tsvc, uid=3)
    await tsvc.tickets.reply(ADMIN, b, "hi")
    await tsvc.tickets.close(ADMIN, c)
    ids = lambda page: sorted(r["id"] for r in page.rows)  # noqa: E731
    assert ids(await tsvc.tickets.list_tickets(ADMIN, status="open", page=0)) == [a]
    assert ids(await tsvc.tickets.list_tickets(ADMIN, status="answered", page=0)) == [b]
    assert ids(await tsvc.tickets.list_tickets(ADMIN, status="closed", page=0)) == [c]
    assert ids(await tsvc.tickets.list_tickets(ADMIN, status=None, page=0)) == [a, b, c]
    counts = await tsvc.tickets.counts(ADMIN)
    assert (counts["open"], counts["answered"]) == (1, 1)


async def test_an_admin_reply_marks_it_answered_and_is_audited_without_the_text(
    app_pool, tsvc, users
):
    tid = await open_ticket(tsvc, text=f"user secret {MARK}")
    res = await tsvc.tickets.reply(ADMIN, tid, f"  support says {MARK}  ")
    assert (res.kind, res.user_id, res.text) == ("ok", 1, f"support says {MARK}")
    assert await app_pool.fetchval("SELECT status FROM support_tickets") == "answered"
    m = await app_pool.fetchrow("SELECT * FROM support_messages WHERE sender = 'admin'")
    assert (m["admin_id"], m["ticket_text"]) == (ADMIN, f"support says {MARK}")
    (a,) = await app_pool.fetch("SELECT * FROM admin_actions")
    assert (a["admin_id"], a["action"], a["target_user_id"]) == (ADMIN, "ticket_reply", 1)
    assert a["detail"] == f"ticket #{tid}" and MARK not in str(dict(a))


@pytest.mark.parametrize(
    ("text", "kind"), [("", "empty"), ("  ", "empty"), ("x" * 801, "too_long")]
)
async def test_an_admin_reply_is_validated(app_pool, tsvc, users, text, kind):
    tid = await open_ticket(tsvc)
    assert (await tsvc.tickets.reply(ADMIN, tid, text)).kind == kind
    assert (await tsvc.tickets.reply(ADMIN, tid + 99, "x")).kind == "not_found"
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 1


async def test_an_admin_close_records_who_and_is_audited(app_pool, tsvc, users):
    tid = await open_ticket(tsvc)
    res = await tsvc.tickets.close(ADMIN, tid)
    assert (res.kind, res.user_id) == ("ok", 1)
    t = await app_pool.fetchrow("SELECT * FROM support_tickets")
    assert (t["status"], t["closed_by"]) == ("closed", "admin") and t["closed_at"]
    assert [r["action"] for r in await app_pool.fetch("SELECT action FROM admin_actions")] == [
        "ticket_close"
    ]
    assert (await tsvc.tickets.close(ADMIN, tid)).kind == "closed"
    assert (await tsvc.tickets.close(ADMIN, tid + 99)).kind == "not_found"


async def test_admin_actions_are_rate_limited(tsvc, clock, users):
    tid = await open_ticket(tsvc)
    kinds = [(await tsvc.tickets.reply(ADMIN, tid, f"r{i}")).kind for i in range(14)]
    assert "rate_limited" in kinds


# ------------------------------------------------------------------ privacy


async def test_ticket_text_is_stored_only_in_support_messages_and_chat_text_never_in_tickets(
    app_pool, tsvc
):
    for uid in (1, 2):
        await make_user(app_pool, uid)
    await start_chat(tsvc, 1, 2)
    await tsvc.relay.relay(1, Incoming("text", text=f"chat line {MARK}"))
    await tsvc.matchmaking.stop(1)
    assert MARK not in await dump_all_tables(app_pool)  # chat text is never stored
    await open_ticket(tsvc, text=f"to support {MARK}")
    hits = []
    for r in await app_pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"):
        rows = await app_pool.fetch(f'SELECT row_to_json(x)::text AS j FROM "{r["tablename"]}" x')  # noqa: S608
        if any(MARK in row["j"] for row in rows):
            hits.append(r["tablename"])
    assert hits == ["support_messages"]


async def test_deleting_the_account_deletes_its_tickets_and_nothing_of_others(
    app_pool, tsvc, users
):
    mine = await open_ticket(tsvc, uid=1, text=f"mine {MARK}")
    await tsvc.tickets.reply(ADMIN, mine, f"reply {MARK}")
    other = await open_ticket(tsvc, uid=2, text="theirs")
    assert (await tsvc.account.delete_account(1)).kind == "deleted"
    assert [r["id"] for r in await app_pool.fetch("SELECT id FROM support_tickets")] == [other]
    assert await app_pool.fetchval("SELECT count(*) FROM support_messages") == 1
    dump = await dump_all_tables(app_pool)
    assert MARK not in dump
    # the audit log keeps only ids and the action
    assert [r["action"] for r in await app_pool.fetch("SELECT action FROM admin_actions")] == [
        "ticket_reply"
    ]
    assert await tsvc.tickets.my_ticket(1, mine) is None


async def test_blocking_a_minor_deletes_their_tickets_too(app_pool, tsvc, users):
    await open_ticket(tsvc, uid=1, text=f"minor {MARK}")
    await tsvc.account.block_underage(1, "age_gate")
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 0
    assert MARK not in await dump_all_tables(app_pool)
