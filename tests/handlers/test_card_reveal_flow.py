"""The 👁 button under a masked chat card: callback pc:r:<session id>, decided at press time."""

from bot import keyboards as kb
from tests.factories import enable_payments, give_tier, make_plan
from tests.handlers.harness import ADMIN_ID

A, B, C = 5252001, 5252002, 5252003  # A: 31 Female Oslo; B: 28 Male Rome; C: bystander


async def meet(tg):
    await tg.onboard(A, name="Asha", age="31", gender="👩 Female", city="Oslo")
    await tg.onboard(B, name="Bob", age="28", gender="👨 Male", city="Rome")
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")


def reveal_data(tg, uid):
    return [d for d in tg.callback_data(uid) if d.startswith("pc:r:")]


def last_alert(tg):
    return tg.callback_answers()[-1]


def edits(out):
    return [o for o in out if o.kind == "EditMessageText"]


def to_user(out, uid):
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id == uid]


async def session_id(pool):
    return await pool.fetchval("SELECT id FROM chat_sessions WHERE ended_at IS NULL")


async def test_a_masked_card_has_a_button_whose_data_is_only_the_session_id(tg, app_pool):
    await meet(tg)
    sid = await session_id(app_pool)
    assert reveal_data(tg, A) == reveal_data(tg, B) == [f"pc:r:{sid}"]
    card = [o for o in tg.out if o.chat_id == A and "Age: ••" in o.text][-1]
    button = card.markup.inline_keyboard[0][0]
    assert button.text == "👁 Show age & gender" and button.callback_data == f"pc:r:{sid}"
    for out in (o for o in tg.out if "••" in o.text):  # the cards of both sides
        shipped = out.text + repr(out.markup)
        for secret in ("31", "28", "Female", "Male"):  # city is free, age and gender are not
            assert secret not in shipped
    # the header carries the chat menu, the card carries the button
    assert tg.texts(A)[-2] == "🎉 <b>Partner Found!</b>" and tg.texts(A)[-1] == card.text


async def test_a_premium_viewer_gets_no_button_and_sees_the_values_at_once(tg, app_pool):
    await tg.onboard(A, name="Asha", age="31", gender="👩 Female", city="Oslo")
    await tg.onboard(B, name="Bob", age="28", gender="👨 Male", city="Rome")
    await give_tier(app_pool, A, "premium")
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    assert reveal_data(tg, A) == [] and "Age: 28" in tg.texts(A)[-1]
    assert len(reveal_data(tg, B)) == 1  # the free partner still has one


async def test_a_free_viewer_gets_the_premium_plans_with_benefits_and_no_values(tg, app_pool):
    await enable_payments(app_pool)
    await make_plan(app_pool)
    await meet(tg)
    sid = await session_id(app_pool)
    out = await tg.press(A, f"pc:r:{sid}")
    alert = last_alert(tg)
    assert alert.show_alert and "Premium" in alert.text
    assert edits(out) == []  # the card is untouched
    (plans,) = to_user(out, A)
    assert "Premium 30 Days" in plans and "₹99 / 30 days" in plans
    assert "200 random chats a day" in plans and "See your partner's age, gender, city" in plans
    assert "/stop" in plans  # a payment cannot start mid-chat
    assert getattr(out[-1].markup, "inline_keyboard", None) is None
    assert "28" not in plans.replace("₹99", "") and "Male" not in plans


async def test_a_free_viewer_with_no_plans_on_sale_still_gets_the_alert_only(tg, app_pool):
    await meet(tg)
    sid = await session_id(app_pool)
    out = await tg.press(A, f"pc:r:{sid}")
    assert last_alert(tg).show_alert and to_user(out, A) == [] and edits(out) == []


async def test_a_viewer_who_upgraded_after_the_card_can_reveal_it(tg, app_pool):
    await meet(tg)
    sid = await session_id(app_pool)
    await give_tier(app_pool, A, "premium")
    out = await tg.press(A, f"pc:r:{sid}")
    (edit,) = edits(out)
    assert "Age: 28" in edit.text and "Gender: 👨 Male" in edit.text and "City: Rome" in edit.text
    assert edit.markup is None  # nothing left to reveal
    # B (still free) is unaffected by A's upgrade
    assert "Age: ••" in tg.texts(B)[-1]


async def test_the_owners_privacy_beats_a_vip_viewer_at_press_time(tg, app_pool):
    await meet(tg)
    sid = await session_id(app_pool)
    await give_tier(app_pool, A, "vip")
    await app_pool.execute("UPDATE profiles SET show_age = false WHERE user_id = $1", B)
    (edit,) = edits(await tg.press(A, f"pc:r:{sid}"))
    assert "Age: 🙈 Hidden by the user" in edit.text and "Gender: 👨 Male" in edit.text
    assert "28" not in edit.text


async def test_a_free_viewer_is_not_sold_what_the_owner_hid(tg, app_pool):
    await enable_payments(app_pool)
    await make_plan(app_pool)
    await meet(tg)
    sid = await session_id(app_pool)
    await app_pool.execute(
        "UPDATE profiles SET show_age = false, show_gender = false WHERE user_id = $1", B
    )
    out = await tg.press(A, f"pc:r:{sid}")
    (edit,) = edits(out)  # nothing to buy: no plan would show these
    assert edit.text.count("🙈 Hidden by the user") >= 2 and "28" not in edit.text
    assert to_user(out, A) == []
    assert edit.markup is None


async def test_a_forged_session_id_shows_nothing(tg, app_pool):
    await meet(tg)
    sid = await session_id(app_pool)
    for forged in (sid + 1, sid + 1000, sid + 2**20):
        out = await tg.press(A, f"pc:r:{forged}")
        assert "ended" in last_alert(tg).text and last_alert(tg).show_alert
        assert edits(out) == [] and to_user(out, A) == []


async def test_someone_outside_the_chat_cannot_use_the_button(tg, app_pool):
    await meet(tg)
    await tg.onboard(C, name="Cy", age="40", gender="🧑 Other", city="Pune")
    await give_tier(app_pool, C, "vip")
    sid = await session_id(app_pool)
    out = await tg.press(C, f"pc:r:{sid}")
    assert "ended" in last_alert(tg).text
    assert edits(out) == [] and to_user(out, C) == []
    assert not any(s in o.text for o in out for s in ("31", "28", "Female", "Male"))


async def test_after_the_chat_ended_the_button_shows_nothing(tg, app_pool):
    await meet(tg)
    sid = await session_id(app_pool)
    await give_tier(app_pool, A, "vip")
    await tg.send(B, "/stop")
    out = await tg.press(A, f"pc:r:{sid}")
    assert "ended" in last_alert(tg).text and last_alert(tg).show_alert
    assert edits([o for o in out if o.kind == "EditMessageText"]) == []
    assert not any("28" in o.text for o in out)


async def test_a_new_chat_does_not_honour_the_old_cards_button(tg, app_pool):
    await meet(tg)
    old = await session_id(app_pool)
    await give_tier(app_pool, A, "vip")
    await tg.send(A, "/next")  # ends the first chat and searches again
    await tg.onboard(C, name="Cy", age="40", gender="🧑 Other", city="Pune")
    await tg.send(C, "/chat")  # A and C now chat
    assert await session_id(app_pool) != old
    out = await tg.press(A, f"pc:r:{old}")
    assert "ended" in last_alert(tg).text and edits(out) == []
    assert not any("28" in o.text for o in out)


async def test_pressing_is_rate_limited(tg, app_pool):
    await meet(tg)
    sid = await session_id(app_pool)
    tg.auto_advance = 0.0
    for _ in range(5):
        await tg.press(A, f"pc:r:{sid}")
        assert "Too many" not in last_alert(tg).text
    await tg.press(A, f"pc:r:{sid}")
    assert "Too many attempts" in last_alert(tg).text


async def test_the_button_and_alerts_are_in_hindi(tg, app_pool):
    await tg.onboard(A, name="Asha", age="31", gender="👩 Female", city="Oslo")
    await tg.onboard(B, name="Bob", age="28", gender="👨 Male", city="Rome")
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", A)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    sid = await session_id(app_pool)
    card = [o for o in tg.out if o.chat_id == A and "••" in o.text][-1]
    assert card.markup.inline_keyboard[0][0].text == "👁 उम्र और जेंडर देखें"
    await tg.press(A, f"pc:r:{sid}")
    assert "प्रीमियम" in last_alert(tg).text
    await tg.press(A, f"pc:r:{sid + 5}")
    assert "खत्म" in last_alert(tg).text


def test_the_callback_is_strictly_parsed():
    assert kb.parse_reveal_cb("pc:r:12") == 12
    for bad in (
        None,
        "",
        "pc:r:",
        "pc:r:-1",
        "pc:r:1:2",
        "pc:r:1a",
        "PC:r:1",
        "pc:x:1",
        "pc:r:" + "9" * 13,
    ):
        assert kb.parse_reveal_cb(bad) is None
    assert len(kb.card_reveal(10**12 - 1, "en").inline_keyboard[0][0].callback_data) <= 64


def test_the_admin_constant_is_not_a_player():
    assert ADMIN_ID not in (A, B, C)
