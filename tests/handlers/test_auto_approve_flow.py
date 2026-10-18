"""Automatic payment approval seen from Telegram: the user is told, the admin switch decides."""

from datetime import UTC, datetime, timedelta

import pytest

from tests import payment_images as img
from tests.factories import enable_payments, make_plan
from tests.handlers.harness import ADMIN_ID

U = 7101
UPI = "shop@okbank"


class FakeOcr:
    name = "fake"

    def __init__(self, text):
        self.text = text

    async def read(self, data, width, height):
        return self.text


def good(amount="₹99"):
    moment = datetime.now(UTC) + timedelta(hours=5, minutes=30) - timedelta(minutes=1)
    return (
        f"Payment Successful\n{amount}\nPaid to\n{UPI}\nUPI Ref No: 412345678901\n"
        f"{moment.strftime('%d %b %Y, %I:%M %p')}\n"
    )


@pytest.fixture
async def shop(app_pool):
    await enable_payments(app_pool, UPI)
    return await make_plan(app_pool)


def to_user(out):
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id == U]


async def pay(tg, plan, text, *, on):
    await tg.onboard(U)
    tg.services.payments._ocr = FakeOcr(text)
    if on:
        assert (
            await tg.services.payments.set_auto_approve(ADMIN_ID, True, confirmed=True)
        ).kind == "ok"
    await tg.press(U, f"pm:s:{plan}")
    return await tg.photo_bytes(U, img.JPEG)


async def test_a_verified_payment_activates_premium_and_tells_the_user(tg, shop, app_pool):
    out = await pay(tg, shop, good(), on=True)
    assert any("approved" in t and "Premium is active" in t for t in to_user(out))
    assert not any("submitted" in t.lower() or "waiting" in t.lower() for t in to_user(out))
    assert await app_pool.fetchval("SELECT approval_mode FROM payments") == "auto"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions WHERE user_id = $1", U) == 1


async def test_with_the_switch_off_the_payment_waits_for_an_admin(tg, shop, app_pool):
    out = await pay(tg, shop, good(), on=False)
    assert not any("approved" in t for t in to_user(out))
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"
    assert any(o.chat_id == ADMIN_ID for o in out)  # the admins are still told to review it


async def test_a_wrong_amount_waits_for_an_admin_even_when_on(tg, shop, app_pool):
    out = await pay(tg, shop, good("₹29"), on=True)
    assert not any("approved" in t for t in to_user(out))
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
