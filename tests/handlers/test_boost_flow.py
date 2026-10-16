"""/boost through the real Dispatcher."""

import re

from tests.factories import give_tier

A, B = 6262001, 6262002


def said(out):
    return [o.text for o in out if o.kind == "SendMessage"]


def callbacks(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


async def test_free_user_sees_the_locked_message_and_no_button(tg, app_pool):
    await tg.onboard(A)
    out = await tg.send(A, "/boost")
    assert said(out) == ["🔒 Profile Boost is available with Pro/VIP: /premium"]
    assert callbacks(out) == []
    await tg.press(A, "bo:go")  # a forged / stale button
    assert await app_pool.fetchval("SELECT count(*) FROM profile_boosts") == 0
    assert any(a.show_alert for a in tg.callback_answers()[-1:])


async def test_premium_is_locked_too(tg, app_pool):
    await tg.onboard(A)
    await give_tier(app_pool, A, "premium")
    assert "Pro/VIP" in said(await tg.send(A, "/boost"))[0]


async def test_pro_activates_and_sees_when_it_expires(tg, app_pool):
    await tg.onboard(A)
    await give_tier(app_pool, A, "pro")
    out = await tg.send(A, "/boost")
    assert "Profile Boost" in said(out)[0] and callbacks(out) == ["bo:go"]
    out = await tg.press(A, "bo:go")
    text = said(out)[0]
    assert "Boost active" in text
    assert re.search(r"Expires: \d\d-\d\d-\d{4} \d\d:\d\d UTC", text)
    # asking again (command or stale button) shows the same expiry, and creates nothing new
    again = said(await tg.send(A, "/boost"))[0]
    assert again == text
    assert said(await tg.press(A, "bo:go"))[0] == text
    assert await app_pool.fetchval("SELECT count(*) FROM profile_boosts") == 1


async def test_cooldown_message_after_the_boost_ended(tg, app_pool):
    await tg.onboard(A)
    await give_tier(app_pool, A, "vip")
    await tg.press(A, "bo:go")
    await app_pool.execute(
        "UPDATE profile_boosts SET started_at = now() - interval '3 hours', "
        "ends_at = now() - interval '2 hours', cooldown_until = now() + interval '9 hours'"
    )
    text = said(await tg.send(A, "/boost"))[0]
    assert "boost again after" in text and "UTC" in text
    assert callbacks(await tg.send(A, "/boost")) == []


async def test_hindi_boost_screens(tg, app_pool):
    await tg.onboard(A)
    await give_tier(app_pool, A, "pro")
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", A)
    assert "प्रोफ़ाइल बूस्ट" in said(await tg.send(A, "/boost"))[0]
    assert "बूस्ट चालू है" in said(await tg.press(A, "bo:go"))[0]


async def test_forged_boost_callbacks_do_nothing(tg, app_pool):
    await tg.onboard(A)
    await give_tier(app_pool, A, "pro")
    for data in ("bo:", "bo:go2", "bo:go:1", "bo:../x"):
        await tg.press(A, data)
    assert await app_pool.fetchval("SELECT count(*) FROM profile_boosts") == 0


async def test_boost_is_refused_while_in_a_chat(tg, app_pool):
    await tg.onboard(A)
    await tg.onboard(B)
    await give_tier(app_pool, A, "pro")
    await tg.send(A, "💬 Random Chat")
    await tg.send(B, "💬 Random Chat")
    out = await tg.send(A, "/boost")
    assert "/stop" in said(out)[0]


async def test_boosted_profile_comes_first_for_other_users(tg, app_pool):
    for uid, name in ((A, "Alma"), (B, "Bela"), (6262003, "Cara")):
        await tg.onboard(uid, name=name)
    await app_pool.execute(
        "UPDATE users SET last_seen_at = now() - interval '1 day' WHERE id = $1", A
    )
    await give_tier(app_pool, A, "pro")
    await app_pool.execute("DELETE FROM subscriptions")  # keep Alma on the Free ranking
    await app_pool.execute(
        "INSERT INTO entitlement_grants (user_id, feature_key, value, source) "
        "VALUES ($1, 'profile_boost', '{\"enabled\": true}', 'admin')",
        A,
    )
    await tg.press(A, "bo:go")
    shown = said(await tg.send(6262003, "/browse"))[0]
    assert "Alma" in shown
