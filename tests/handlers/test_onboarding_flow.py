"""18+ gate and profile onboarding, end to end through the real Dispatcher."""

import pytest
from aiogram.types import Contact, Dice, Document, Location, MessageEntity, Voice

from bot.db import repo_profiles, repo_users
from bot.texts import en as t


def cbs(out):
    return [
        c
        for o in out
        for c in (b.callback_data for row in getattr(o.markup, "inline_keyboard", []) for b in row)
    ]


async def status(pool, uid):
    row = await repo_users.get(pool, uid)
    return row["status"] if row else None


# ---------------- the gate ----------------


async def test_new_user_gets_the_age_gate_and_no_profile_questions_yet(tg, app_pool):
    out = await tg.send(1, "/start", username="newbie")
    assert out[0].text == t.AGE_GATE
    assert cbs(out) == ["age:yes", "age:no"]
    assert await status(app_pool, 1) == "onboarding"
    assert (await repo_users.get(app_pool, 1))["age_confirmed_at"] is None


async def test_confirming_18_starts_the_profile_flow(tg, app_pool):
    await tg.send(1, "/start")
    out = await tg.press(1, "age:yes")
    assert t.ASK_NAME in [o.text for o in out]
    assert (await repo_users.get(app_pool, 1))["age_confirmed_at"] is not None


async def test_typing_answers_without_confirming_age_does_not_skip_the_gate(tg, app_pool):
    await tg.send(1, "/start")
    for text in ("Alex", "25", "/chat"):
        out = await tg.send(1, text)
        assert out[0].text == t.FINISH_SETUP
    assert await repo_profiles.get(app_pool, 1) is None
    assert await status(app_pool, 1) == "onboarding"


async def test_pressing_age_yes_without_start_does_nothing(tg, app_pool):
    out = await tg.press(1, "age:yes")
    assert await repo_users.get(app_pool, 1) is None
    assert not any(o.chat_id == 1 and o.kind == "SendMessage" and o.text == t.ASK_NAME for o in out)


async def test_under_18_button_hard_blocks_and_removes_all_access(tg, app_pool):
    await tg.send(1, "/start", username="kid")
    out = await tg.press(1, "age:no")
    assert any(o.text == t.BLOCKED_ADULTS_ONLY for o in out)
    assert await status(app_pool, 1) == "blocked_underage"
    assert (await repo_users.get(app_pool, 1))["username"] is None
    assert await repo_users.tombstone_reason(app_pool, 1) == "underage"

    for text in ("/start", "/chat", "hello", "/profile", "/report"):
        out = await tg.send(1, text)
        assert out[0].text == t.BLOCKED_ADULTS_ONLY
    out = await tg.press(1, "age:yes")  # trying to flip the decision later
    assert await status(app_pool, 1) == "blocked_underage"
    assert (await repo_users.get(app_pool, 1))["age_confirmed_at"] is None


@pytest.mark.parametrize("age", ["17", "1", "0", "١٧"])
async def test_typing_an_age_under_18_hard_blocks(tg, app_pool, age):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    await tg.send(1, "Alex")
    out = await tg.send(1, age)
    assert out[0].text == t.BLOCKED_ADULTS_ONLY
    assert await status(app_pool, 1) == "blocked_underage"
    assert await repo_profiles.get(app_pool, 1) is None
    assert (await tg.send(1, "/start"))[0].text == t.BLOCKED_ADULTS_ONLY


# ---------------- the flow ----------------


async def test_complete_onboarding_creates_an_active_profile(tg, app_pool):
    out = await tg.onboard(
        1, name="Alex", age="25", gender="👩 Female", looking="👨 Men", city="Berlin", bio="Hi!"
    )
    assert out[0].text == t.PROFILE_SAVED
    assert await status(app_pool, 1) == "active"
    p = await repo_profiles.get(app_pool, 1)
    assert (p["name"], p["age"], p["gender"], p["looking_for"], p["city"], p["bio"]) == (
        "Alex",
        25,
        "f",
        "m",
        "Berlin",
        "Hi!",
    )


async def test_prompts_come_in_order(tg):
    await tg.send(1, "/start")
    assert t.ASK_NAME in [o.text for o in await tg.press(1, "age:yes")]
    assert (await tg.send(1, "Alex"))[0].text == t.ASK_AGE
    assert (await tg.send(1, "25"))[0].text == t.ASK_GENDER
    assert (await tg.send(1, "👨 Male"))[0].text == t.ASK_LOOKING
    assert (await tg.send(1, "👥 Everyone"))[0].text == t.ASK_CITY
    assert (await tg.send(1, "Berlin"))[0].text == t.ASK_BIO


@pytest.mark.parametrize(
    "answer,expected",
    [
        ("abc", "age_not_number"),
        ("twenty", "age_not_number"),
        ("-5", "age_not_number"),
        ("1.5", "age_not_number"),
        ("99", "age_range"),
        ("150", "age_range"),
        ("", "age_not_number"),
    ],
)
async def test_invalid_age_is_reprompted_without_blocking(tg, app_pool, answer, expected):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    await tg.send(1, "Alex")
    out = await tg.send(1, answer or " ")
    assert out[0].text == t.INVALID[expected]
    assert await status(app_pool, 1) == "onboarding"
    assert (await tg.send(1, "30"))[0].text == t.ASK_GENDER  # same step still works


@pytest.mark.parametrize(
    "step,answer,key",
    [
        ("name", "x" * 31, "name_length"),
        ("name", "visit foo.com", "text_link"),
        ("name", "@bobby", "text_contact"),
        ("city", "c" * 41, "city_length"),
        ("city", "t.me/x", "text_link"),
        ("bio", "b" * 301, "bio_length"),
        ("bio", "call 9876543210", "text_contact"),
        ("bio", "I'm 15", "text_minor"),
        ("bio", "send nudes", "text_sexual"),
        ("bio", "check http://x.com", "text_link"),
    ],
)
async def test_bad_text_answers_are_rejected_and_the_step_repeats(tg, step, answer, key):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    if step != "name":
        await tg.send(1, "Alex")
        await tg.send(1, "25")
        await tg.send(1, "👨 Male")
        await tg.send(1, "👥 Everyone")
    if step == "bio":
        await tg.send(1, "Berlin")
    out = await tg.send(1, answer)
    assert out[0].text == t.INVALID[key]


async def test_non_text_answers_and_wrong_buttons_are_refused(tg):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    assert (await tg.sticker(1))[0].text == t.SEND_TEXT
    assert (await tg.photo(1))[0].text == t.SEND_TEXT
    await tg.send(1, "Alex")
    await tg.send(1, "25")
    assert (await tg.send(1, "robot"))[0].text == t.USE_BUTTONS
    assert (await tg.photo(1))[0].text == t.USE_BUTTONS
    assert (await tg.send(1, "Male"))[0].text == t.USE_BUTTONS  # must be the exact button label


async def test_commands_are_never_taken_as_answers(tg, app_pool):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    out = await tg.send(1, "/chat")
    assert out[0].text == t.FINISH_SETUP  # not interpreted as a name
    assert (await tg.send(1, "Alex"))[0].text == t.ASK_AGE  # the name step is still waiting


async def test_start_in_the_middle_restarts_at_the_name_without_regating(tg):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    await tg.send(1, "Alex")
    out = await tg.send(1, "/start")
    assert out[0].text == t.ASK_NAME


async def test_cancel_leaves_the_flow(tg):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    assert (await tg.send(1, "/cancel"))[0].text == t.PROFILE_CANCELLED
    assert (await tg.send(1, "Alex"))[0].text == t.FINISH_SETUP


async def test_lost_conversation_state_after_a_restart_is_recoverable(tg, app_pool):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    tg.dp.fsm.storage.storage.clear()  # what a bot restart does to MemoryStorage
    assert (await tg.send(1, "Alex"))[0].text == t.FINISH_SETUP
    assert (await tg.send(1, "/start"))[0].text == t.ASK_NAME  # age already confirmed: no re-gate
    assert (await repo_users.get(app_pool, 1))["age_confirmed_at"] is not None


async def test_whitespace_in_answers_is_normalised(tg, app_pool):
    await tg.onboard(1, name="  Anna   Maria  ", city="  New   York ")
    p = await repo_profiles.get(app_pool, 1)
    assert (p["name"], p["city"]) == ("Anna Maria", "New York")


# ---------------- profile viewing / editing / output safety ----------------


async def test_profile_output_escapes_html(tg):
    await tg.onboard(1, name="<b>Al</b>", city="A&B <i>", bio="x <script>alert(1)</script> & y")
    out = await tg.send(1, "/profile")
    text = out[0].text
    assert "&lt;b&gt;Al&lt;/b&gt;" in text and "&lt;script&gt;" in text and "A&amp;B" in text
    assert "<script>" not in text


async def test_edit_profile_changes_data_and_keeps_user_active(tg, app_pool):
    await tg.onboard(1, city="Berlin")
    assert (await tg.send(1, "/editprofile"))[0].text == t.ASK_NAME
    for answer in ("Alex2", "26", "👨 Male", "👥 Everyone", "Paris", "New bio"):
        await tg.send(1, answer)
    p = await repo_profiles.get(app_pool, 1)
    assert (p["city"], p["age"], p["name"]) == ("Paris", 26, "Alex2")
    assert await status(app_pool, 1) == "active"


async def test_editing_the_age_to_under_18_blocks_the_user(tg, app_pool):
    await tg.onboard(1)
    await tg.send(1, "/editprofile")
    await tg.send(1, "Alex")
    out = await tg.send(1, "16")
    assert out[0].text == t.BLOCKED_ADULTS_ONLY
    assert await status(app_pool, 1) == "blocked_underage"
    assert await repo_profiles.get(app_pool, 1) is None


async def test_edit_profile_is_unavailable_to_onboarding_users_and_in_chat(tg):
    await tg.send(1, "/start")
    assert (await tg.send(1, "/editprofile"))[0].text == t.FINISH_SETUP
    await tg.onboard(2)
    await tg.onboard(3)
    await tg.send(2, "/chat")
    await tg.send(3, "/chat")
    assert (await tg.send(2, "/editprofile"))[0].text == t.EDIT_NOT_IN_CHAT


# ---------------- who may talk to the bot at all ----------------


async def test_group_chats_and_bots_are_ignored(tg, app_pool):
    assert await tg.send_in_group(1, "/start") == []
    assert await tg.send_as_bot(2, "/start") == []
    out = await tg.press_in_group(3, "age:yes")
    assert [o.kind for o in out] == ["AnswerCallbackQuery"]  # just stops the spinner, nothing else
    assert [await repo_users.get(app_pool, u) for u in (1, 2, 3)] == [None, None, None]


async def test_unknown_user_must_start_first(tg):
    assert (await tg.send(5, "hello"))[0].text == t.NEED_START
    assert (await tg.send(5, "/chat"))[0].text == t.NEED_START


async def test_active_user_unknown_command_gets_hint(tg):
    await tg.onboard(1)
    # plain text outside a chat is not relayed anywhere
    assert (await tg.send(1, "hello?"))[0].text == t.NOT_IN_CHAT


@pytest.mark.parametrize(
    "fields",
    [
        {"voice": Voice(file_id="v", file_unique_id="uv", duration=1)},
        {"document": Document(file_id="d", file_unique_id="ud")},
        {"location": Location(latitude=1.0, longitude=2.0)},
        {"contact": Contact(phone_number="123", first_name="x")},
        {"dice": Dice(emoji="🎲", value=3)},
    ],
)
async def test_media_during_onboarding_is_refused(tg, fields):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    out = await tg.raw(1, **fields)
    assert out[0].text == t.SEND_TEXT


async def test_link_entities_in_onboarding_text_are_not_special_cased_but_text_is_checked(tg):
    await tg.send(1, "/start")
    await tg.press(1, "age:yes")
    out = await tg.send(
        1,
        "click",
        entities=[MessageEntity(type="text_link", offset=0, length=5, url="http://x.io")],
    )
    # plain "click" is harmless text; the hidden-link entity is dropped, only text is kept
    assert out[0].text == t.ASK_AGE
