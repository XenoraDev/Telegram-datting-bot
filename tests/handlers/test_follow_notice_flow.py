"""The "someone followed you" push and the Follow back button."""

from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import SendMessage

from bot.texts.i18n import tr
from tests.factories import give_tier
from tests.handlers.harness import ADMIN_ID

A, B, C, D = 7600001, 7600002, 7600003, 7600004
NAMES = {A: "Ann", B: "Bob", C: "Cee", D: "Dee"}


async def people(tg, *uids):
    for uid in uids:
        await tg.onboard(uid, name=NAMES[uid], city="Rome", bio="hello")


async def handle(pool, uid):
    return (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", uid)).hex


async def follow(tg, pool, who, target):
    return await tg.press(who, f"lk:{await handle(pool, target)}")


def to(out, uid):
    return [o for o in out if o.kind == "SendMessage" and o.chat_id == uid]


PUSH_MARKERS = ("Someone followed you", "New follower", "किसी ने आपको फ़ॉलो किया", "नया फ़ॉलोअर")


def pushes(tg, uid):
    """Every "someone followed you" message sent to uid so far."""
    return [
        o
        for o in tg.out
        if o.kind == "SendMessage" and o.chat_id == uid and any(m in o.text for m in PUSH_MARKERS)
    ]


def buttons(o):
    return [b for row in (getattr(o.markup, "inline_keyboard", None) or []) for b in row]


# ------------------------------------------------------------------ Free: a count, no identity


async def test_a_free_user_is_told_someone_followed_them_with_a_count_and_no_identity(tg, app_pool):
    await people(tg, A, B)
    out = await follow(tg, app_pool, A, B)
    (push,) = pushes(tg, B)
    assert "Someone followed you" in push.text and "waiting for you: 1" in push.text
    assert "/followers" in push.text and "/premium" in push.text
    assert buttons(push) == [] and getattr(push.markup, "inline_keyboard", None) is None
    for secret in ("Ann", str(A), f"user{A}", "@", "Rome"):
        assert secret not in push.text
    assert to(out, A)  # the follower's own flow carried on (next profile / empty state)


async def test_free_users_get_at_most_one_push_every_six_hours_but_the_count_grows(tg, app_pool):
    await people(tg, A, B, C, D)
    tg.auto_advance = 1.0
    await follow(tg, app_pool, A, B)
    await follow(tg, app_pool, C, B)
    assert len(pushes(tg, B)) == 1  # the second one is held back
    tg.clock.advance(6 * 3600)
    await follow(tg, app_pool, D, B)
    second = pushes(tg, B)
    assert len(second) == 2 and "waiting for you: 3" in second[1].text


async def test_the_push_is_in_the_receivers_language(tg, app_pool):
    await people(tg, A, B)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", B)
    await follow(tg, app_pool, A, B)
    (push,) = pushes(tg, B)
    assert push.text == tr("follow.new_free", "hi", n=1)


# ------------------------------------------------------------------ Pro: the card and Follow back


async def test_a_pro_user_gets_the_followers_card_and_a_follow_back_button(tg, app_pool):
    await people(tg, A, B)
    await give_tier(app_pool, B, "pro")
    await follow(tg, app_pool, A, B)
    (push,) = pushes(tg, B)
    assert "New follower" in push.text and "Ann" in push.text and "hello" in push.text
    assert [b.text for b in buttons(push)] == ["➕ Follow back", "⏭️ Skip", "🚨 Report", "🚫 Block"]
    pid = await handle(app_pool, A)
    assert [b.callback_data for b in buttons(push)] == [
        f"lk:{pid}",
        f"ps:{pid}",
        f"rp:{pid}",
        f"bk:{pid}",
    ]
    for b in buttons(push):
        assert str(A) not in b.callback_data and "Ann" not in b.callback_data
    assert str(A) not in push.text and f"user{A}" not in push.text and "@" not in push.text


async def test_the_followers_privacy_stands_against_a_receiver_of_the_same_tier(tg, app_pool):
    await people(tg, A, B)
    await give_tier(app_pool, A, "pro")
    await give_tier(app_pool, B, "pro")
    await app_pool.execute("UPDATE profiles SET show_city = false WHERE user_id = $1", A)
    await follow(tg, app_pool, A, B)
    (push,) = pushes(tg, B)
    assert "Hidden by the user" in push.text and "Rome" not in push.text  # owner privacy stands


async def test_a_higher_tier_receiver_sees_what_a_lower_tier_follower_hid(tg, app_pool):
    await people(tg, A, B)
    await give_tier(app_pool, B, "pro")  # the follower A is Free
    await app_pool.execute("UPDATE profiles SET show_city = false WHERE user_id = $1", A)
    await follow(tg, app_pool, A, B)
    (push,) = pushes(tg, B)
    assert "Rome" in push.text and "Hidden by the user" not in push.text
    assert str(A) not in push.text and "@" not in push.text  # no id, no username, no contact


async def test_follow_back_makes_friends_exactly_once(tg, app_pool):
    await people(tg, A, B)
    await give_tier(app_pool, B, "pro")
    await follow(tg, app_pool, A, B)
    pid = await handle(app_pool, A)
    out = await tg.press(B, f"lk:{pid}")  # the Follow back button
    assert any("You are now friends" in o.text for o in to(out, B))
    assert any("You are now friends" in o.text for o in to(out, A))  # the follower is told too
    assert await app_pool.fetchval("SELECT count(*) FROM matches") == 1
    await tg.press(B, f"lk:{pid}")  # pressing it again changes nothing
    assert await app_pool.fetchval("SELECT count(*) FROM matches") == 1
    assert await app_pool.fetchval("SELECT count(*) FROM swipes WHERE action = 'like'") == 2


async def test_pro_pushes_are_rate_limited_after_a_burst(tg, app_pool):
    await people(tg, A, B)
    await give_tier(app_pool, B, "pro")
    for uid in range(7700001, 7700013):  # twelve followers
        await tg.onboard(uid, name=f"F{uid - 7700000}", city="Rome")
        await follow(tg, app_pool, uid, B)
    assert len(pushes(tg, B)) == 10


# ------------------------------------------------------------------ when nothing is sent


async def test_no_push_for_a_mutual_follow_a_pass_a_duplicate_or_while_in_a_chat(tg, app_pool):
    await people(tg, A, B, C)
    await follow(tg, app_pool, B, A)  # B follows A first
    await follow(tg, app_pool, A, B)  # mutual: friends, not a "followed you" push
    assert pushes(tg, B) == [] and any(
        "You are now friends" in o.text for o in tg.out if o.chat_id == B
    )
    await tg.press(C, f"ps:{await handle(app_pool, B)}")  # a skip is not a follow
    assert pushes(tg, B) == []
    await follow(tg, app_pool, C, A)
    await follow(tg, app_pool, C, A)  # a duplicate press
    assert len(pushes(tg, A)) == 1
    # in a chat: nothing now, the follow is still in /followers
    await people(tg, D)
    await tg.send(B, "/chat")
    await tg.send(D, "/chat")
    before = len(pushes(tg, D))
    await follow(tg, app_pool, C, D)
    assert len(pushes(tg, D)) == before
    await tg.send(D, "/stop")
    assert "follow you" in " ".join(o.text for o in to(await tg.send(D, "/followers"), D))


async def test_blocked_or_banned_people_cannot_trigger_a_push(tg, app_pool):
    from bot.db import repo_safety

    await people(tg, A, B, C)
    await repo_safety.add_block(app_pool, B, A)  # B blocked A
    await follow(tg, app_pool, A, B)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", C)
    await follow(tg, app_pool, A, C)
    assert pushes(tg, B) == [] and pushes(tg, C) == []
    assert await app_pool.fetchval("SELECT count(*) FROM swipes") == 0


async def test_an_unreachable_receiver_or_a_failing_push_never_breaks_the_follow(
    tg, app_pool, monkeypatch
):
    await people(tg, A, B, C)
    tg.fail_chat[B] = TelegramForbiddenError(SendMessage(chat_id=B, text="x"), "bot was blocked")
    out = await follow(tg, app_pool, A, B)
    assert to(out, A) and await app_pool.fetchval("SELECT count(*) FROM swipes") == 1
    await give_tier(app_pool, C, "pro")

    async def boom(*a, **kw):
        raise RuntimeError("cards are down")

    monkeypatch.setattr(tg.services.cards, "for_viewer", boom)
    out = await follow(tg, app_pool, A, C)
    assert to(out, A)  # Ann still gets her next screen
    assert await app_pool.fetchval("SELECT count(*) FROM swipes WHERE target_id = $1", C) == 1
    assert [m for m in tg.texts(ADMIN_ID) if "Bot error" in m] == []  # swallowed and logged only
