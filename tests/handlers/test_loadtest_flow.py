"""The Telegram half of the load-test start protection, through the real dispatcher."""

import json

import pytest

from bot.handlers.loadtest import confirmation_text, send_confirmation
from bot.loadtest.config import TestConfig
from bot.loadtest.safety import LoadTestSettings
from tests.handlers.harness import ADMIN_ID, FakeTelegram
from tests.loadtest.conftest import FORM, env_for, mark_database

OTHER = 5551234


@pytest.fixture
async def lt_tg(app_pool, dsn):
    await mark_database(app_pool, dsn)
    async with FakeTelegram(
        app_pool, loadtest_settings=LoadTestSettings.from_env(env_for(dsn))
    ) as fake:
        await fake.onboard(ADMIN_ID)  # an admin who can be messaged has started the bot
        yield fake


async def request(tg):
    req = await tg.services.loadtest.request_confirmation(ADMIN_ID, FORM)
    assert req.kind == "ok", req
    ok = await send_confirmation(
        tg.bot, tg.services, ADMIN_ID, req.confirmation_id, req.token, req.config
    )
    assert ok
    return req


def edited(tg):
    return [o for o in tg.out if o.kind == "EditMessageText"]


async def test_the_confirmation_message_shows_the_exact_configuration(lt_tg):
    req = await request(lt_tg)
    sent = lt_tg.inbox(ADMIN_ID)[-1]
    for needle in (
        "LOAD TEST CONFIRMATION",
        "TESTING",
        "Chats: 50",
        "Messages / chat / min: 40",
        "Duration: 1 minute",
        "Estimated messages: 2,167",
    ):
        assert needle in sent.text, (needle, sent.text)
    assert "significant server resources" in sent.text
    buttons = [b for row in sent.markup.inline_keyboard for b in row]
    assert [b.text for b in buttons] == ["✅ CONFIRM LOAD TEST", "❌ CANCEL"]
    assert buttons[0].callback_data == f"lt:y:{req.token}" and len(buttons[0].callback_data) <= 64
    assert await lt_tg.services.loadtest.confirmation_state(ADMIN_ID) is not None
    assert await lt_tg.services.loadtest.runs(ADMIN_ID) == []  # nothing started yet


async def test_pressing_confirm_queues_exactly_one_run(lt_tg, app_pool):
    req = await request(lt_tg)
    await lt_tg.press(ADMIN_ID, f"lt:y:{req.token}")
    runs = await lt_tg.services.loadtest.runs(ADMIN_ID)
    assert len(runs) == 1 and runs[0]["status"] == "queued"
    assert runs[0]["config"] == req.config.as_json()
    assert "Confirmed" in edited(lt_tg)[-1].text and "#" in edited(lt_tg)[-1].text
    await lt_tg.press(ADMIN_ID, f"lt:y:{req.token}")  # a second press does nothing
    assert len(await lt_tg.services.loadtest.runs(ADMIN_ID)) == 1
    assert "no longer valid" in edited(lt_tg)[-1].text


async def test_cancel_starts_nothing(lt_tg):
    req = await request(lt_tg)
    await lt_tg.press(ADMIN_ID, f"lt:n:{req.token}")
    assert await lt_tg.services.loadtest.runs(ADMIN_ID) == []
    assert "cancelled" in edited(lt_tg)[-1].text
    await lt_tg.press(ADMIN_ID, f"lt:y:{req.token}")
    assert await lt_tg.services.loadtest.runs(ADMIN_ID) == []


async def test_someone_else_pressing_the_button_does_nothing(lt_tg):
    req = await request(lt_tg)
    await lt_tg.onboard(OTHER)
    before = len(edited(lt_tg))
    await lt_tg.press(OTHER, f"lt:y:{req.token}")
    assert await lt_tg.services.loadtest.runs(ADMIN_ID) == []
    assert len(edited(lt_tg)) == before  # the admin's message is untouched
    assert any("administrators" in (a.text or "") for a in lt_tg.callback_answers())
    state = await lt_tg.services.loadtest.confirmation_state(ADMIN_ID)
    assert state["state"] == "pending"


@pytest.mark.parametrize(
    "data", ["lt:y:short", "lt:y:", "lt:z:" + "a" * 32, "lt:y:" + "!" * 32, "lt:"]
)
async def test_malformed_callbacks_are_ignored(lt_tg, data):
    await request(lt_tg)
    await lt_tg.press(ADMIN_ID, data)
    assert await lt_tg.services.loadtest.runs(ADMIN_ID) == []


async def test_an_expired_confirmation_is_reported(lt_tg, app_pool):
    req = await request(lt_tg)
    await app_pool.execute(
        "UPDATE load_test_confirmations SET created_at = now() - interval '9 minutes', "
        "expires_at = now() - interval '4 minutes'"
    )
    await lt_tg.press(ADMIN_ID, f"lt:y:{req.token}")
    assert "expired" in edited(lt_tg)[-1].text
    assert await lt_tg.services.loadtest.runs(ADMIN_ID) == []


async def test_a_blocked_environment_is_reported_in_telegram(app_pool, dsn):
    await mark_database(app_pool, dsn)
    settings = LoadTestSettings.from_env(env_for(dsn))
    async with FakeTelegram(app_pool, loadtest_settings=settings) as tg:
        await tg.onboard(ADMIN_ID)
        req = await request(tg)
        await app_pool.execute('COMMENT ON DATABASE "%s" IS NULL' % dsn.rsplit("/", 1)[1])  # noqa: UP031
        await tg.press(ADMIN_ID, f"lt:y:{req.token}")
        assert "LOAD TEST BLOCKED" in edited(tg)[-1].text
        assert await tg.services.loadtest.runs(ADMIN_ID) == []


async def test_a_second_test_cannot_be_confirmed_while_one_is_active(lt_tg):
    first = await request(lt_tg)
    second = await request(lt_tg)  # supersedes the first token
    await lt_tg.press(ADMIN_ID, f"lt:y:{second.token}")
    third = await lt_tg.services.loadtest.request_confirmation(ADMIN_ID, FORM)
    assert third.kind == "busy"
    await lt_tg.press(ADMIN_ID, f"lt:y:{first.token}")
    assert len(await lt_tg.services.loadtest.runs(ADMIN_ID)) == 1


async def test_a_failed_delivery_invalidates_the_confirmation(lt_tg):
    req = await lt_tg.services.loadtest.request_confirmation(ADMIN_ID, FORM)
    lt_tg.unreachable.add(ADMIN_ID)
    ok = await send_confirmation(
        lt_tg.bot, lt_tg.services, ADMIN_ID, req.confirmation_id, req.token, req.config
    )
    assert ok is False
    assert (await lt_tg.services.loadtest.confirmation_state(ADMIN_ID))["state"] == "failed"


async def test_the_hindi_confirmation(lt_tg, app_pool):
    await lt_tg.onboard(ADMIN_ID)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", ADMIN_ID)
    lt_tg.services.language.invalidate(ADMIN_ID) if hasattr(
        lt_tg.services.language, "invalidate"
    ) else None
    req = await request(lt_tg)
    sent = lt_tg.inbox(ADMIN_ID)[-1]
    assert "लोड टेस्ट की पुष्टि" in sent.text and "परीक्षण" in sent.text
    assert [b.text for row in sent.markup.inline_keyboard for b in row] == [
        "✅ लोड टेस्ट की पुष्टि करें",
        "❌ रद्द करें",
    ]
    await lt_tg.press(ADMIN_ID, f"lt:y:{req.token}")
    assert "पुष्टि हो गई" in edited(lt_tg)[-1].text


def test_text_formats_whole_minutes_and_seconds():
    cfg = TestConfig("custom", 250, 50, 300, 60)
    text = confirmation_text(cfg, "en")
    assert "Duration: 5 minutes" in text and "Ramp-up: 1 minute" in text
    assert "Estimated messages: 68,750" in text
    assert "Duration: 90 seconds" in confirmation_text(TestConfig("custom", 1, 1, 90, 0), "en")
    assert json.dumps(cfg.as_json())
