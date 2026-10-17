"""P2 through the real Dispatcher: browse card, like / pass buttons, matches, report / block."""

import pytest

from bot import keyboards as kb
from bot.db import repo_safety
from bot.texts import en as t

A, B, C = 5151001, 5151002, 5151003


async def handle(pool, uid: int) -> str:
    return (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", uid)).hex


def cbs(out):
    return [
        b.callback_data
        for o in out
        for row in getattr(o.markup, "inline_keyboard", [])
        for b in row
    ]


async def test_menu_has_browse_and_matches_buttons(tg):
    out = await tg.onboard(A)
    labels = [b.text for row in out[-1].markup.keyboard for b in row]
    assert kb.BTN_BROWSE in labels and kb.BTN_MATCHES in labels and kb.BTN_CHAT in labels


async def test_browse_shows_a_clean_card_with_five_buttons(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B, name="Bea", age="29", gender="👩 Female", city="Delhi", bio="Hi <b>x</b>")
    out = await tg.send(A, kb.BTN_BROWSE)
    assert len(out) == 1
    text = out[0].text
    assert "Bea" in text and "29" in text and "Delhi" in text and "Hi &lt;b&gt;x&lt;/b&gt;" in text
    for secret in (f"{B}", "user5151002", "@"):
        assert secret not in text
    pid = await handle(app_pool, B)
    assert cbs(out) == [f"lk:{pid}", f"ps:{pid}", f"rp:{pid}", f"bk:{pid}", "fl:h"]
    assert not any(str(B) in c or str(A) in c for c in tg.callback_data())


async def test_browse_command_works_and_empty_state_is_friendly(tg):
    await tg.onboard(A)
    assert (await tg.send(A, "/browse"))[0].text == t.BROWSE_EMPTY


async def test_like_records_the_swipe_and_shows_the_next_card(tg, app_pool):
    for uid in (A, B, C):
        await tg.onboard(uid)
    first = await tg.send(A, "/browse")
    data = cbs(first)[0]
    out = await tg.press(A, data)
    assert await app_pool.fetchval("SELECT action FROM swipes WHERE swiper_id = $1", A) == "like"
    assert any("🎂" in o.text for o in out)  # the next card arrived
    assert await app_pool.fetchval("SELECT count(*) FROM matches") == 0


async def test_pass_then_list_ends(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    out = await tg.send(A, "/browse")
    out = await tg.press(A, cbs(out)[1])
    assert await app_pool.fetchval("SELECT action FROM swipes") == "pass"
    assert [o.text for o in out if o.kind == "SendMessage"] == [t.BROWSE_EMPTY]


async def test_double_pressing_a_like_button_is_harmless(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    data = cbs(await tg.send(A, "/browse"))[0]
    await tg.press(A, data)
    await tg.press(A, data)
    assert await app_pool.fetchval("SELECT count(*) FROM swipes") == 1


async def test_mutual_like_notifies_both_without_any_contact_details(tg, app_pool):
    await tg.onboard(A, name="Ann")
    await tg.onboard(B, name="Bob", city="Rome")
    await tg.press(A, f"lk:{await handle(app_pool, B)}")
    out = await tg.press(B, f"lk:{await handle(app_pool, A)}")
    assert await app_pool.fetchval("SELECT count(*) FROM matches") == 1
    to_b = [o for o in out if o.chat_id == B and o.kind == "SendMessage"][0]
    to_a = [o for o in out if o.chat_id == A and o.kind == "SendMessage"][0]
    assert "It's a Match" in to_a.text and "Bob" in to_a.text and "Rome" in to_a.text
    assert "It's a Match" in to_b.text and "Ann" in to_b.text
    for o in (to_a, to_b):
        assert "user51510" not in o.text and "@" not in o.text and f"{A}" not in o.text
        assert cbs([o]) == ["mm", "br"]


async def test_unreachable_partner_does_not_break_the_match(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.press(A, f"lk:{await handle(app_pool, B)}")
    tg.unreachable.add(A)
    out = await tg.press(B, f"lk:{await handle(app_pool, A)}")
    assert await app_pool.fetchval("SELECT count(*) FROM matches") == 1
    assert any("It's a Match" in o.text for o in out if o.chat_id == B)


async def test_my_matches_lists_matches_with_report_and_block_only(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B, name="Bob", city="Rome")
    assert (await tg.send(A, kb.BTN_MATCHES))[0].text == t.MATCHES_NONE
    await tg.press(A, f"lk:{await handle(app_pool, B)}")
    await tg.press(B, f"lk:{await handle(app_pool, A)}")
    out = [o for o in await tg.press(A, "mm") if o.kind == "SendMessage"]
    texts = [o.text for o in out]
    assert "Your matches" in texts[0] and "(1)" in texts[0]
    assert "Bob" in texts[1] and "Rome" in texts[1] and "@" not in texts[1]
    pid = await handle(app_pool, B)
    assert cbs(out[1:]) == [f"mr:{pid}", f"mk:{pid}"]
    assert [o.text for o in await tg.send(A, "/matches")][1:2] == [texts[1]]


async def test_report_from_browse_goes_through_the_existing_report_system(tg, app_pool):
    for uid in (A, B, C):
        await tg.onboard(uid)
    first = await tg.send(A, "/browse")
    pid = cbs(first)[2].split(":")[1]
    prompt = await tg.press(A, f"rp:{pid}")
    assert prompt[0].text == t.REPORT_START
    assert f"rq:{pid}:spam" in cbs(prompt)
    out = await tg.press(A, f"rq:{pid}:spam")
    assert t.REPORT_CREATED in [o.text for o in out]
    assert await app_pool.fetchval("SELECT source FROM reports") == "profile"
    assert any("🎂" in o.text for o in out)  # carries on with the next profile
    reported = await app_pool.fetchval("SELECT reported_id FROM reports")
    assert await repo_safety.is_blocked_either_way(app_pool, A, reported)


async def test_block_from_browse_and_matches(tg, app_pool):
    for uid in (A, B, C):
        await tg.onboard(uid)
    first = await tg.send(A, "/browse")
    pid = cbs(first)[3].split(":")[1]
    out = await tg.press(A, f"bk:{pid}")
    assert t.PROFILE_BLOCKED in [o.text for o in out]
    assert await app_pool.fetchval("SELECT count(*) FROM blocks WHERE blocker_id = $1", A) == 1
    # blocking from the matches list does not push a new card
    other = (
        C if await app_pool.fetchval("SELECT count(*) FROM blocks WHERE blocked_id = $1", B) else B
    )
    await tg.press(A, f"lk:{await handle(app_pool, other)}")
    await tg.press(other, f"lk:{await handle(app_pool, A)}")
    out = await tg.press(A, f"mk:{await handle(app_pool, other)}")
    assert [o.text for o in out if o.kind == "SendMessage"] == [t.PROFILE_BLOCKED]
    assert (await tg.send(A, "/matches"))[0].text == t.MATCHES_NONE


async def test_blocking_the_current_chat_partner_from_a_card_ends_the_chat(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    out = await tg.press(A, f"bk:{await handle(app_pool, B)}")
    assert t.CHAT_ENDED_PARTNER in [o.text for o in out if o.chat_id == B]
    assert await tg.services.matchmaking.current_chat(A) is None


async def test_browse_is_refused_during_a_chat_and_chat_text_is_not_swallowed(tg):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    assert (await tg.send(A, "/browse"))[0].text == t.BROWSE_IN_CHAT
    out = await tg.send(A, "hello there")
    assert [o.chat_id for o in out] == [B]


@pytest.mark.parametrize(
    "data",
    [
        "lk:",
        "lk:xyz",
        "lk:" + "0" * 32,
        "lk:" + "G" * 32,
        "lk:" + "a" * 33,
        "mm:extra",
        "rq:" + "a" * 32 + ":",
        "rq:" + "a" * 32 + ":BAD!",
        "zz:" + "a" * 32,
        "ps:5151002",
    ],
)
async def test_hostile_or_stale_profile_buttons_change_nothing(tg, app_pool, data):
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.press(A, data)
    for table in ("swipes", "matches", "reports", "blocks"):
        assert await app_pool.fetchval(f"SELECT count(*) FROM {table}") == 0  # noqa: S608


async def test_profile_buttons_do_nothing_for_users_who_are_not_onboarded(tg, app_pool):
    await tg.onboard(B)
    await tg.send(A, "/start")  # age gate only
    await tg.press(A, f"lk:{await handle(app_pool, B)}")
    assert await app_pool.fetchval("SELECT count(*) FROM swipes") == 0
    out = await tg.send(A, kb.BTN_BROWSE)  # no browsing: the sign-up is picked up again instead
    assert [o.text for o in out if o.kind == "SendMessage"][-1] == t.AGE_GATE
    assert await app_pool.fetchval("SELECT count(*) FROM swipes") == 0


async def test_liking_a_profile_that_was_deleted_meanwhile_is_handled(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    data = cbs(await tg.send(A, "/browse"))[0]
    await tg.press(B, "del:yes")
    out = await tg.press(A, data)
    assert t.PROFILE_GONE in [o.text for o in out]
    assert await app_pool.fetchval("SELECT count(*) FROM swipes") == 0
