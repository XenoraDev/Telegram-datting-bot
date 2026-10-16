"""The paying user never sees what the analyser read, whatever the verdict."""

import shutil

import pytest

from tests import evidence_images as pics
from tests.factories import enable_payments, make_plan
from tests.handlers.harness import ADMIN_ID

U = 7771


def cbs(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
@pytest.mark.parametrize("amount", ["99.00", "49.00"])
async def test_the_user_is_told_nothing_about_the_screenshot_analysis(tg, app_pool, amount):
    await enable_payments(app_pool)
    plan = await make_plan(app_pool)
    await tg.onboard(U)
    out = await tg.press(U, f"pm:s:{plan}")
    pid = int([c for c in cbs(out) if c.startswith("pm:u:")][0].split(":")[2])
    await tg.press(U, f"pm:u:{pid}")
    shot = pics.screenshot(pics.payment_lines(amount=amount))
    out = await tg.photo_bytes(U, shot)
    out += await tg.send(U, "412345678901")
    to_user = " ".join(o.text for o in out if o.kind == "SendMessage" and o.chat_id == U)
    for hidden in ("VALIDATION", "mismatch", "Mismatch", "amount", "Tesseract", "49.00", "analys"):
        assert hidden not in to_user
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence_analysis") == 1
    assert await app_pool.fetchval("SELECT status FROM payments WHERE id = $1", pid) == "pending"
    alerts = " ".join(o.text for o in out if o.kind == "SendMessage" and o.chat_id == ADMIN_ID)
    assert "VALIDATION" not in alerts and "49.00" not in alerts
