"""/deleteme and the behaviour of blocked / banned / deleted accounts."""

from bot import keyboards as kb
from bot.db import repo_chat, repo_profiles, repo_users
from bot.texts import en as t

A, B = 5150001, 5150002


async def status(pool, uid):
    row = await repo_users.get(pool, uid)
    return row["status"] if row else None


async def test_deleteme_asks_for_confirmation_and_changes_nothing_yet(tg, app_pool):
    await tg.onboard(A)
    out = await tg.send(A, "/deleteme")
    assert out[0].text == t.DELETE_CONFIRM
    assert tg.callback_data(A)[-2:] == ["del:yes", "del:no"]
    assert await status(app_pool, A) == "active"
    assert await repo_profiles.get(app_pool, A) is not None


async def test_cancel_keeps_the_account(tg, app_pool):
    await tg.onboard(A)
    await tg.send(A, "/deleteme")
    out = await tg.press(A, "del:no")
    assert t.DELETE_CANCELLED in [o.text for o in out]
    assert await status(app_pool, A) == "active"


async def test_confirmed_deletion_removes_the_data(tg, app_pool):
    await tg.onboard(A)
    await tg.send(A, "/start", username="alice")
    await tg.send(A, "/deleteme")
    out = await tg.press(A, "del:yes")
    assert t.DELETE_DONE in [o.text for o in out]
    assert await status(app_pool, A) == "deleted"
    row = await repo_users.get(app_pool, A)
    assert row["username"] is None and row["age_confirmed_at"] is None
    assert await repo_profiles.get(app_pool, A) is None


async def test_deleted_user_cannot_continue_normal_use(tg, app_pool):
    await tg.onboard(A)
    await tg.press(A, "del:yes")
    for text in ("/chat", "/profile", "/next", "/stop", "/report", "/help", "hello", kb.BTN_CHAT):
        out = await tg.send(A, text)
        assert out[0].text == t.ACCOUNT_DELETED, text
    out = await tg.press(A, "age:yes")  # old buttons are dead too
    assert await status(app_pool, A) == "deleted"
    assert (await repo_users.get(app_pool, A))["age_confirmed_at"] is None
    out = await tg.press(A, "rr:1:spam")
    assert not [o for o in out if o.kind == "SendMessage" and o.text == t.REPORT_CREATED]


async def test_deleted_user_starting_again_is_a_new_account_that_needs_the_gate(tg, app_pool):
    await tg.onboard(A)
    await tg.press(A, "del:yes")
    out = await tg.send(A, "/start")
    assert out[0].text == t.AGE_GATE
    assert (await tg.send(A, "/chat"))[0].text == t.FINISH_SETUP
    await tg.press(A, "age:yes")
    await tg.send(A, "New")
    await tg.send(A, "40")
    await tg.send(A, "👩 Female")
    await tg.send(A, "👥 Everyone")
    await tg.send(A, "🌍 Other")
    await tg.send(A, "Wien")
    await tg.send(A, "fresh start")
    assert await status(app_pool, A) == "active"
    assert (await repo_profiles.get(app_pool, A))["name"] == "New"


async def test_deleting_during_a_chat_ends_it_and_tells_the_partner(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    out = await tg.press(A, "del:yes")
    assert t.CHAT_ENDED_PARTNER in [o.text for o in out if o.chat_id == B]
    assert await repo_chat.get_partner(app_pool, B) is None
    assert (await tg.send(B, "still there?"))[0].text == t.NOT_IN_CHAT


async def test_a_delete_button_only_ever_deletes_the_person_who_pressed_it(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/deleteme")  # A opens the dialog...
    await tg.press(B, "del:yes")  # ...but B presses a button
    assert await status(app_pool, A) == "active"
    assert await status(app_pool, B) == "deleted"


async def test_deleting_twice_is_harmless(tg, app_pool):
    await tg.onboard(A)
    await tg.press(A, "del:yes")
    await tg.press(A, "del:yes")
    # the second press is stopped by the account gate: a deleted account has no valid buttons
    assert t.ACCOUNT_DELETED in [a.text for a in tg.callback_answers()]
    assert await status(app_pool, A) == "deleted"


async def test_deleteme_requests_are_rate_limited(tg):
    await tg.onboard(A)
    tg.auto_advance = 1
    texts = [(await tg.send(A, "/deleteme"))[0].text for _ in range(5)]
    assert t.DELETE_RATE_LIMITED in texts


async def test_unknown_user_cannot_delete_or_use_the_confirm_button(tg, app_pool):
    out = await tg.press(31337, "del:yes")
    assert await repo_users.get(app_pool, 31337) is None
    assert not [o for o in out if o.text in (t.DELETE_DONE, t.DELETE_ALREADY)]


# ---------------- blocked / banned accounts ----------------


async def test_underage_user_can_still_erase_their_data_but_stays_blocked(tg, app_pool):
    await tg.send(A, "/start", username="kid")
    await tg.press(A, "age:no")
    assert (await tg.send(A, "/deleteme"))[0].text == t.DELETE_CONFIRM
    out = await tg.press(A, "del:yes")
    assert t.DELETE_DONE_RESTRICTED in [o.text for o in out]
    assert await status(app_pool, A) == "blocked_underage"
    assert (await tg.send(A, "/start"))[0].text == t.BLOCKED_ADULTS_ONLY
    assert await repo_users.tombstone_reason(app_pool, A) == "underage"


async def test_banned_user_is_locked_out_of_everything_but_deleteme(tg, app_pool):
    await tg.onboard(A)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", A)
    for text in ("/start", "/chat", "hello", "/profile", "/report", kb.BTN_NEXT):
        assert (await tg.send(A, text))[0].text == t.BLOCKED_BANNED, text
    assert (await tg.send(A, "/deleteme"))[0].text == t.DELETE_CONFIRM
    await tg.press(A, "del:yes")
    assert await status(app_pool, A) == "banned"  # data wiped, ban stays


async def test_a_user_banned_mid_chat_is_cut_off_immediately(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", A)
    out = await tg.send(A, "let me talk")
    assert [(o.chat_id, o.text) for o in out] == [(A, t.BLOCKED_BANNED)]
    out = await tg.send(B, "hello?")
    assert [(o.chat_id, o.text) for o in out] == [(B, t.NOT_IN_CHAT)]  # nothing reaches A


async def test_blocked_user_cannot_use_buttons(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    sid = (await tg.services.matchmaking.current_chat(A))[0]
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", A)
    await tg.press(A, f"rr:{sid}:spam")
    await tg.press(A, f"blk:{sid}")
    assert await app_pool.fetchval("SELECT count(*) FROM reports") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM blocks") == 0
