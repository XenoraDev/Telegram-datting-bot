"""/filters and the 🎯 button through the real Dispatcher: gating, menus, tampering."""

from tests.factories import give_tier

A, B, C = 6161001, 6161002, 6161003


def edits(out):
    return [o for o in out if o.kind == "EditMessageText"]


def callbacks(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


async def setup(tg):
    await tg.onboard(A, name="Asha", age="31", gender="👩 Female", city="Pune")
    await tg.onboard(B, name="Bina", age="27", gender="👩 Female", city="Delhi")
    await tg.onboard(C, name="Chet", age="45", gender="👨 Male", city="Pune")


async def test_free_user_gets_a_localized_subscription_message(tg, app_pool):
    await setup(tg)
    out = await tg.send(A, "/filters")
    assert any("Advanced Filters are available with Pro or VIP" in o.text for o in out)
    assert not callbacks(out)
    # the button under a browse card leads to the same wall, and stores nothing
    out = await tg.press(A, "fl:a:1")
    assert any(a.show_alert and "Pro or VIP" in (a.text or "") for a in tg.callback_answers())
    assert await app_pool.fetchval("SELECT count(*) FROM browse_filters") == 0
    assert not edits(out)


async def test_free_user_hindi_message(tg, app_pool):
    await setup(tg)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", A)
    out = await tg.send(A, "/filters")
    assert any("प्रो या वीआईपी" in o.text for o in out)


async def test_browse_card_carries_the_filters_button(tg, app_pool):
    await setup(tg)
    out = await tg.send(A, "/browse")
    assert "fl:h" in callbacks(out)


async def test_pro_opens_picks_applies_and_resets(tg, app_pool):
    await setup(tg)
    await give_tier(app_pool, A, "pro")
    out = await tg.send(A, "/filters")
    assert {"fl:a", "fl:g", "fl:c", "fl:k", "fl:r", "fl:z"} <= set(callbacks(out))

    out = await tg.press(A, "fl:g")  # gender sub-menu
    assert edits(out) and {"fl:g:m", "fl:g:f", "fl:g:o", "fl:g:x"} <= set(callbacks(out))
    out = await tg.press(A, "fl:g:m")
    assert "Male" in edits(out)[-1].text
    assert await app_pool.fetchval("SELECT gender FROM browse_filters WHERE user_id=$1", A) == "m"

    out = await tg.send(A, "/browse")
    shown = [o.text for o in out if o.kind == "SendMessage"]
    assert any("Chet" in x for x in shown) and not any("Bina" in x for x in shown)

    out = await tg.press(A, "fl:r")
    assert await app_pool.fetchval("SELECT count(*) FROM browse_filters") == 0
    assert any("Any" in o.text for o in edits(out))


async def test_age_preset_city_text_and_empty_message(tg, app_pool):
    await setup(tg)
    await give_tier(app_pool, A, "vip")
    await tg.press(A, "fl:a:1")  # 25-29
    row = await app_pool.fetchrow("SELECT age_min, age_max FROM browse_filters WHERE user_id=$1", A)
    assert (row["age_min"], row["age_max"]) == (25, 29)

    out = await tg.press(A, "fl:c")  # asks for the city
    assert any("Type the city" in o.text for o in edits(out))
    out = await tg.send(A, "Delhi")
    assert await app_pool.fetchval("SELECT city FROM browse_filters WHERE user_id=$1", A) == "Delhi"
    assert any("Delhi" in o.text for o in out)

    out = await tg.send(A, "/browse")
    assert any("Bina" in o.text for o in out if o.kind == "SendMessage")
    handle = await app_pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", B)
    await tg.press(A, f"ps:{handle.hex}")
    out = await tg.send(A, "/browse")
    assert any("No profiles match your filters" in o.text for o in out)


async def test_bad_city_text_is_refused_and_not_stored(tg, app_pool):
    await setup(tg)
    await give_tier(app_pool, A, "pro")
    await tg.press(A, "fl:c")
    out = await tg.send(A, "City 12345")
    assert await app_pool.fetchval("SELECT count(*) FROM browse_filters") == 0
    assert out and out[-1].kind == "SendMessage"


async def test_tampered_callbacks_change_nothing(tg, app_pool):
    await setup(tg)
    await give_tier(app_pool, A, "pro")
    for data in ("fl:a:9", "fl:g:z", "fl:a:../..", "fl:k:" + "x" * 40, "fl:", "fl:a:-1", "fl:c:1"):
        await tg.press(A, data)
    assert await app_pool.fetchval("SELECT count(*) FROM browse_filters") == 0


async def test_pro_whose_plan_ended_is_locked_again_on_the_next_press(tg, app_pool):
    await setup(tg)
    sub = await give_tier(app_pool, A, "pro")
    await tg.press(A, "fl:g:f")
    assert await app_pool.fetchval("SELECT gender FROM browse_filters WHERE user_id=$1", A) == "f"
    await app_pool.execute("UPDATE subscriptions SET current_period_end = now() WHERE id=$1", sub)
    await tg.press(A, "fl:g:m")
    assert await app_pool.fetchval("SELECT gender FROM browse_filters WHERE user_id=$1", A) == "f"
    assert any(a.show_alert for a in tg.callback_answers()[-1:])
    out = await tg.send(A, "/browse")  # the stored filter is not applied any more
    shown = [o.text for o in out if o.kind == "SendMessage"]
    assert any("Chet" in x for x in shown) or any("Bina" in x for x in shown)
