"""Partner cards and /privacy through the real Dispatcher: chat, mutual match, matches list."""

import pytest

from bot import keyboards as kb
from tests.factories import give_tier

A, B = 5252001, 5252002
FREE_LOCK_EN = "••"


def last(tg, uid):
    return tg.texts(uid)[-1]


def callbacks(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


async def meet(tg):
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")


async def onboard_pair(tg):
    await tg.onboard(A, name="Asha", age="31", gender="👩 Female", city="Oslo", bio="likes tea")
    await tg.onboard(B, name="Bob", age="28", gender="👨 Male", city="Rome", bio="likes jazz")


async def handle(pool, uid):
    return (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", uid)).hex


# ------------------------------------------------------------------ random chat start


async def test_free_users_get_the_full_card_with_age_and_gender_locked(tg):
    await onboard_pair(tg)
    await meet(tg)
    to_a, to_b = last(tg, A), last(tg, B)
    # the header (with the chat menu) and the card (with the 👁 button) are two messages
    assert tg.texts(A)[-2] == tg.texts(B)[-2] == "🎉 <b>Partner Found!</b>"
    for text, city in ((to_a, "Rome"), (to_b, "Oslo")):
        for line in (
            "Age: ••",
            "Gender: ••",
            f"City: {city}",
            "Country: 🌍 Other",
            "🔒 Anonymous chat",
        ):
            assert line in text
    for secret in ("28", "31", "Male", "Female", "👨", "👩", "Asha", "Bob", "tea", "jazz"):
        assert secret not in to_a + to_b


async def test_a_premium_viewer_sees_the_partners_age_and_gender_but_the_free_partner_does_not(
    tg, app_pool
):
    await onboard_pair(tg)
    await give_tier(app_pool, A, "premium")
    await meet(tg)
    assert "Age: 28" in last(tg, A) and "Gender: 👨 Male" in last(tg, A)
    assert "🔒" not in last(tg, A).replace("🔒 Anonymous chat", "")
    assert "Age: ••" in last(tg, B) and "31" not in last(tg, B)
    assert "⭐ Premium" in last(tg, B)  # B sees A's badge


@pytest.mark.parametrize("tier", ["pro", "vip"])
async def test_pro_and_vip_viewers_see_everything_the_owner_allows(tg, app_pool, tier):
    await onboard_pair(tg)
    await give_tier(app_pool, B, tier)
    await meet(tg)
    assert (
        "Age: 31" in last(tg, B)
        and "Gender: 👩 Female" in last(tg, B)
        and "City: Oslo" in last(tg, B)
    )


async def test_privacy_off_beats_even_a_vip_subscription(tg, app_pool):
    await onboard_pair(tg)
    await give_tier(app_pool, B, "vip")
    await app_pool.execute(
        "UPDATE profiles SET show_age = false, show_city = false WHERE user_id = $1", A
    )
    await meet(tg)
    to_b = last(tg, B)
    assert "Age: 🙈 Hidden by the user" in to_b and "City: 🙈 Hidden by the user" in to_b
    assert "Gender: 👩 Female" in to_b  # only what the owner switched off is hidden
    assert "31" not in to_b and "Oslo" not in to_b and FREE_LOCK_EN not in to_b


async def test_the_card_is_in_each_users_own_language(tg, app_pool):
    await onboard_pair(tg)
    await tg.press(A, "lg:hi")
    await meet(tg)
    assert "उम्र: ••" in last(tg, A) and "शहर: Rome" in last(tg, A)
    assert "Age: ••" in last(tg, B)


async def test_the_upgrade_hint_is_not_repeated_on_every_new_chat(tg):
    await onboard_pair(tg)
    await tg.onboard(5252003, name="Cy", age="40", gender="👨 Male", city="Pune")
    await meet(tg)
    assert "/premium" in last(tg, A)
    await tg.send(A, "/stop")
    await tg.send(A, "/chat")
    await tg.send(5252003, "/chat")
    assert "Age: ••" in last(tg, A) and "City: Pune" in last(tg, A)
    assert "/premium" not in last(tg, A)  # still locked, but the nudge was already given


async def test_no_protected_value_is_in_any_button_data(tg):
    await onboard_pair(tg)
    out = await tg.send(A, "/chat")
    out += await tg.send(B, "/chat")
    out += await tg.send(A, "/stop")
    data = " ".join(callbacks(out) + callbacks(tg.out))
    for secret in ("31", "28", "Oslo", "Rome", "Female", "Male"):
        assert secret not in data


# ------------------------------------------------------------------ mutual match + matches list


async def test_mutual_match_cards_follow_the_same_rules(tg, app_pool):
    await onboard_pair(tg)
    await give_tier(app_pool, A, "premium")
    await tg.press(A, f"lk:{await handle(app_pool, B)}")
    await tg.press(B, f"lk:{await handle(app_pool, A)}")
    to_a, to_b = last(tg, A), last(tg, B)
    assert (
        "It's a Match" in to_a
        and "Name: Bob" in to_a
        and "Age: 28" in to_a
        and "Gender: 👨 Male" in to_a
    )
    assert "Name: Asha" in to_b and "Age: ••" in to_b and "31" not in to_b
    assert "Looking for:" in to_a and "About: likes jazz" in to_a
    assert "⭐ Premium" in to_b  # A's badge


async def make_matches(tg, app_pool, viewer, others):
    for other in others:
        await app_pool.execute(
            "INSERT INTO matches (user_a_id, user_b_id) VALUES ($1, $2)",
            min(viewer, other),
            max(viewer, other),
        )


async def test_matches_list_has_the_full_structure_and_exactly_one_hint(tg, app_pool):
    await onboard_pair(tg)
    await tg.onboard(5252003, name="Cy", age="40", gender="👨 Male", city="Pune")
    await make_matches(tg, app_pool, A, [B, 5252003])
    out = await tg.send(A, kb.BTN_MATCHES)
    texts = [o.text for o in out if o.kind == "SendMessage"]
    cards = [t for t in texts if "Partner Profile" in t]
    assert len(cards) == 2
    for card in cards:
        for field in (
            "Name:",
            "Age: ••",
            "Gender: ••",
            "City:",
            "Country:",
            "Looking for:",
            "About:",
        ):
            assert field in card
    assert sum("/premium" in t for t in texts) == 1  # one hint for the list, not one per card
    assert all(c[:3] in ("mc:", "mr:", "mk:") for c in callbacks(out))  # 💬 Chat, Report, Block
    assert "40" not in " ".join(texts) and "28" not in " ".join(texts)
    again = [o.text for o in await tg.send(A, kb.BTN_MATCHES) if o.kind == "SendMessage"]
    assert not any("/premium" in t for t in again)  # not nagged again right away


async def test_matches_list_of_a_paid_viewer_has_values_and_no_hint(tg, app_pool):
    await onboard_pair(tg)
    await give_tier(app_pool, A, "pro")
    await make_matches(tg, app_pool, A, [B])
    texts = [o.text for o in await tg.send(A, kb.BTN_MATCHES) if o.kind == "SendMessage"]
    card = next(t for t in texts if "Partner Profile" in t)
    assert "Age: 28" in card and "Gender: 👨 Male" in card and "City: Rome" in card
    assert not any("/premium" in t for t in texts)


# ------------------------------------------------------------------ /privacy


async def test_privacy_screen_toggles_persist_and_use_stable_callbacks(tg, app_pool):
    await tg.onboard(A)
    out = await tg.send(A, "/privacy")
    assert callbacks(out) == ["pv:age", "pv:gender", "pv:city"]
    labels = [b.text for row in out[-1].markup.inline_keyboard for b in row]
    assert labels == ["Age: ✅ Shown", "Gender: ✅ Shown", "City: ✅ Shown"]
    await tg.press(A, "pv:age")
    await tg.press(A, "pv:city")
    row = await app_pool.fetchrow("SELECT show_age, show_gender, show_city FROM profiles")
    assert tuple(row) == (False, True, False)
    edited = [o for o in tg.out if type(o.method).__name__ == "EditMessageReplyMarkup"][-1]
    assert [b.text for r in edited.markup.inline_keyboard for b in r] == [
        "Age: 🙈 Hidden",
        "Gender: ✅ Shown",
        "City: 🙈 Hidden",
    ]
    await tg.press(A, "pv:age")
    assert await app_pool.fetchval("SELECT show_age FROM profiles") is True


async def test_privacy_screen_in_hindi_has_the_same_callbacks(tg):
    await tg.onboard(A)
    await tg.press(A, "lg:hi")
    out = await tg.send(A, "/privacy")
    assert callbacks(out) == ["pv:age", "pv:gender", "pv:city"]
    assert [b.text for row in out[-1].markup.inline_keyboard for b in row][0] == "उम्र: ✅ दिखता है"
    assert "प्राइवेसी" in last(tg, A)


@pytest.mark.parametrize("data", ["pv:name", "pv:", "pv:age;x", "pv:AGE", "pv:bio"])
async def test_forged_privacy_callbacks_change_nothing(tg, app_pool, data):
    await tg.onboard(A)
    await tg.press(A, data)
    assert (
        tuple(await app_pool.fetchrow("SELECT show_age, show_gender, show_city FROM profiles"))
        == (True,) * 3
    )


async def test_privacy_needs_a_finished_profile_and_only_changes_your_own(tg, app_pool):
    await tg.send(A, "/start")
    out = await tg.send(A, "/privacy")
    assert callbacks(out) == []
    await onboard_pair(tg)
    await tg.press(A, "pv:age")
    assert await app_pool.fetchval("SELECT show_age FROM profiles WHERE user_id = $1", B) is True
    assert await app_pool.fetchval("SELECT show_age FROM profiles WHERE user_id = $1", A) is False
