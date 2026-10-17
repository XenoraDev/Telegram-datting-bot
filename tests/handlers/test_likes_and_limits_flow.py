"""/likes (who liked me) and the daily-limit messages through the real Dispatcher."""

import json

from bot import keyboards as kb
from tests.factories import give_tier

A, B, C, D = 5353001, 5353002, 5353003, 5353004


def callbacks(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


def said(out, uid=None):
    return [o.text for o in out if o.kind == "SendMessage" and (uid is None or o.chat_id == uid)]


async def handle(pool, uid):
    return (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", uid)).hex


async def set_limit(pool, plan, feature, limit):
    await pool.execute(
        "UPDATE plan_entitlements pe SET value = $3::jsonb FROM plans p "
        "WHERE p.id = pe.plan_id AND p.code = $1 AND pe.feature_key = $2",
        plan,
        feature,
        json.dumps({"enabled": True, "daily_limit": limit}),
    )


async def likes_setup(tg, app_pool):
    await tg.onboard(A, name="Asha", age="31", gender="👩 Female", city="Oslo")
    await tg.onboard(B, name="Bob", age="28", gender="👨 Male", city="Rome")
    await tg.onboard(C, name="Cy", age="40", gender="👨 Male", city="Pune")
    for liker in (B, C):
        await tg.press(liker, f"lk:{await handle(app_pool, A)}")


# ------------------------------------------------------------------ /likes


async def test_free_users_see_how_many_liked_them_but_not_who(tg, app_pool):
    await likes_setup(tg, app_pool)
    out = await tg.send(A, "/likes")
    text = said(out)[-1]
    assert "2 people follow you" in text and "/premium" in text
    assert callbacks(out) == []
    for secret in ("Bob", "Cy", "28", "40", "Rome", "Pune"):
        assert secret not in text


async def test_a_pro_viewer_sees_who_liked_them_and_can_answer(tg, app_pool):
    await likes_setup(tg, app_pool)
    await give_tier(app_pool, A, "pro")
    out = await tg.send(A, "/likes")
    texts = said(out)
    assert "People who follow you" in texts[0] and "(2)" in texts[0]
    cards = [t for t in texts if "Partner Profile" in t]
    assert len(cards) == 2
    joined = " ".join(cards)
    for shown in ("Name: Bob", "Name: Cy", "Age: 28", "Age: 40", "City: Rome", "City: Pune"):
        assert shown in joined
    assert all(c.split(":")[0] in ("lk", "ps", "rp", "bk") for c in callbacks(out))
    out = await tg.press(A, f"lk:{await handle(app_pool, B)}")  # like back -> match
    assert any("You are now friends" in t for t in said(out, A))
    assert "Name: Bob" not in " ".join(said(await tg.send(A, "/likes")))  # answered: gone


async def test_a_vip_viewer_also_sees_them_and_privacy_still_applies(tg, app_pool):
    await likes_setup(tg, app_pool)
    await give_tier(app_pool, A, "vip")
    await app_pool.execute("UPDATE profiles SET show_age = false WHERE user_id = $1", B)
    texts = " ".join(said(await tg.send(A, "/likes")))
    assert "Age: 🙈 Hidden by the user" in texts and "Age: 40" in texts and "Age: 28" not in texts


async def test_blocked_and_answered_people_are_not_listed(tg, app_pool):
    await likes_setup(tg, app_pool)
    await give_tier(app_pool, A, "pro")
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, $2)", A, B)
    await tg.press(A, f"ps:{await handle(app_pool, C)}")
    out = await tg.send(A, "/likes")
    assert "No new followers are waiting" in said(out)[-1]


async def test_likes_in_hindi_and_when_nobody_liked(tg, app_pool):
    await tg.onboard(A)
    assert "No new followers are waiting" in said(await tg.send(A, "/likes"))[-1]
    await tg.press(A, "lg:hi")
    assert "फ़ॉलोअर" in said(await tg.send(A, "/likes"))[-1]


async def test_likes_is_blocked_during_a_chat(tg, app_pool):
    await likes_setup(tg, app_pool)
    await tg.send(A, kb.BTN_CHAT)
    await tg.send(D, "/start")
    await tg.onboard(D)
    await tg.send(D, kb.BTN_CHAT)
    assert "Leave your current chat" in said(await tg.send(A, "/likes"))[-1]


# ------------------------------------------------------------------ limits in the UI


async def test_the_chat_limit_message_is_shown_in_english_and_hindi(tg, app_pool):
    await set_limit(app_pool, "free", "random_chat", 1)
    await tg.onboard(A)
    await tg.onboard(B)
    await tg.send(A, "/chat")
    await tg.send(A, "/stop")
    out = await tg.send(A, "/chat")
    text = said(out)[-1]
    assert "used all 1 random chats" in text and "00:00 UTC" in text and "/premium" in text
    assert [b.text for r in out[-1].markup.keyboard for b in r][
        0
    ] == kb.BTN_CHAT  # back to the menu
    await tg.press(A, "lg:hi")
    out = await tg.send(A, "/chat")
    assert "सभी 1 रैंडम चैट पूरे हो गए" in said(out)[-1]


async def test_the_browse_and_like_limit_messages(tg, app_pool):
    await set_limit(app_pool, "free", "browse", 1)
    await set_limit(app_pool, "free", "swipe", 1)
    await tg.onboard(A)
    for uid in (B, C, D):
        await tg.onboard(uid)
    out = await tg.send(A, "/browse")
    assert len(callbacks(out)) == 5  # a card with its buttons (+ the filters button)
    out = await tg.send(A, "/browse")
    assert "viewed 1 profiles today" in said(out)[-1] and callbacks(out) == []
    # likes: one allowed, the next press is refused (the card was shown before the limit changed)
    await set_limit(app_pool, "free", "browse", 50)
    first = await tg.send(A, "/browse")
    await tg.press(A, callbacks(first)[0])
    second = await tg.send(A, "/browse")
    out = await tg.press(A, callbacks(second)[0])
    assert any("used all 1 follows" in t for t in said(out))
    assert await app_pool.fetchval("SELECT count(*) FROM swipes WHERE swiper_id = $1", A) == 1


async def test_a_vip_is_slowed_down_not_blocked_by_a_daily_limit(tg, app_pool):
    await tg.onboard(A)
    await give_tier(app_pool, A, "vip")
    for uid in (B, C):
        await tg.onboard(uid)
    tg.auto_advance = 0.0  # presses without any pause: only the abuse guards can slow a VIP
    texts = []
    for _ in range(40):
        texts += said(await tg.send(A, "/browse"))
    assert any("Slow down" in t for t in texts)
    assert not any("daily limit" in t or "used all" in t or "viewed" in t for t in texts)
