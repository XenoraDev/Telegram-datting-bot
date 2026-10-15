"""🌐 Language selector through the real Dispatcher."""

import pytest

from bot import keyboards as kb
from bot.db import repo_users
from bot.texts import en, hi
from tests.handlers.harness import ADMIN_ID

U1 = 6100001


def cbs(out):
    return [
        b.callback_data
        for o in out
        for row in getattr(o.markup, "inline_keyboard", [])
        for b in row
    ]


def labels(out):
    return [b.text for o in out for row in getattr(o.markup, "inline_keyboard", []) for b in row]


async def test_language_command_shows_two_buttons(tg):
    await tg.onboard(U1)
    out = await tg.send(U1, "/language")
    assert out[0].text == en.MESSAGES["lang.prompt"]
    assert cbs(out) == ["lg:en", "lg:hi"]
    assert labels(out) == ["🇬🇧 English", "🇮🇳 हिन्दी"]


async def test_menu_has_a_language_button_that_opens_the_selector(tg):
    out = await tg.onboard(U1)
    menu = [b.text for row in out[-1].markup.keyboard for b in row]
    assert kb.BTN_LANGUAGE in menu
    assert cbs(await tg.send(U1, kb.BTN_LANGUAGE)) == ["lg:en", "lg:hi"]


async def test_choosing_hindi_saves_it_and_confirms_in_hindi(tg, app_pool):
    await tg.onboard(U1)
    out = await tg.press(U1, "lg:hi")
    assert hi.MESSAGES["lang.changed"] in [o.text for o in out]
    assert (await repo_users.get(app_pool, U1))["language"] == "hi"
    # the selector now speaks Hindi, and switching back works
    assert (await tg.send(U1, "/language"))[0].text == hi.MESSAGES["lang.prompt"]
    out = await tg.press(U1, "lg:en")
    assert en.MESSAGES["lang.changed"] in [o.text for o in out]
    assert (await repo_users.get(app_pool, U1))["language"] == "en"


async def test_a_new_user_defaults_to_english_regardless_of_telegram_locale(tg, app_pool):
    await tg.send(U1, "/start")  # Telegram's language_code is never read
    assert (await repo_users.get(app_pool, U1))["language"] == "en"


async def test_selector_works_during_onboarding(tg, app_pool):
    await tg.send(U1, "/start")
    await tg.press(U1, "age:yes")
    assert cbs(await tg.send(U1, "/language")) == ["lg:en", "lg:hi"]
    await tg.press(U1, "lg:hi")
    assert (await repo_users.get(app_pool, U1))["language"] == "hi"


@pytest.mark.parametrize(
    "data", ["lg:", "lg:fr", "lg:HI", "lg:hi:x", "lg:en;DROP TABLE users", "lg"]
)
async def test_hostile_language_payloads_change_nothing(tg, app_pool, data):
    await tg.onboard(U1)
    await tg.press(U1, data)
    assert (await repo_users.get(app_pool, U1))["language"] == "en"


async def test_banned_or_unknown_users_cannot_use_the_selector(tg, app_pool):
    out = await tg.send(U1, "/language")  # no account yet
    assert en.MESSAGES["lang.prompt"] not in [o.text for o in out]
    await tg.press(U1, "lg:hi")
    assert await repo_users.get(app_pool, U1) is None
    await tg.onboard(U1)
    await tg.onboard(ADMIN_ID)
    await tg.press(ADMIN_ID, f"ad:bo:{U1}:0")
    await tg.press(U1, "lg:hi")
    assert (await repo_users.get(app_pool, U1))["language"] == "en"


async def test_help_lists_the_command(tg):
    await tg.onboard(U1)
    assert "/language" in (await tg.send(U1, "/help"))[0].text
