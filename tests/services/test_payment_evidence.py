"""Screenshot evidence: validation is unchanged, and an advisory analysis is stored for the admin.

Real OCR (Tesseract) on real pixels, and fake engines for the failure paths."""

import logging
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from bot.services import build_services
from bot.services.evidence import MISMATCH, OK, UNREADABLE, WARNING
from bot.services.ocr import OcrError, TesseractOcr
from tests import evidence_images as pics
from tests import payment_images as tiny
from tests.factories import enable_payments, make_plan, make_user

needs_ocr = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
U, ADMIN = 8100, 424242


def stamp(minutes_ago: int = 1) -> str:
    """Now, as printed on an Indian UPI app."""
    moment = datetime.now(UTC) + timedelta(hours=5, minutes=30) - timedelta(minutes=minutes_ago)
    return moment.strftime("%d %b %Y, %I:%M %p")


class FakeOcr:
    name = "fake"

    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, 0

    async def read(self, data, width, height):
        self.calls += 1
        if self.error:
            raise self.error
        return self.text


async def order(pool, ocr, clock):
    await enable_payments(pool, "shop@okbank")
    await make_user(pool, U)
    plan = await make_plan(pool, price_minor=9900)
    svc = build_services(pool, clock, clock, frozenset({ADMIN}), ocr=ocr)
    started = await svc.payments.start(U, plan)
    assert started.kind == "ok"
    return svc, started.order.payment_id


async def analysis(pool, pid):
    return await pool.fetchrow("SELECT * FROM payment_evidence_analysis WHERE payment_id = $1", pid)


# ---------------------------------------------------------------- real OCR


@needs_ocr
async def test_a_normal_payment_screenshot_is_ok(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    shot = pics.screenshot(pics.payment_lines(when=stamp(), upi="shop@okbank"))
    assert (await svc.payments.attach_evidence(U, pid, shot)).kind == "ok"
    row = await analysis(app_pool, pid)
    assert row["verdict"] == OK, dict(row)
    assert (row["amount_minor"], row["payee"], row["utr"]) == (9900, "shop@okbank", "412345678901")
    assert row["engine"] == "tesseract" and row["success_marker"] is True


@needs_ocr
@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
async def test_every_accepted_format_can_be_read(app_pool, clock, fmt):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    shot = pics.screenshot(pics.payment_lines(when=stamp()), fmt=fmt)
    assert (await svc.payments.attach_evidence(U, pid, shot)).kind == "ok"
    assert (await analysis(app_pool, pid))["verdict"] == OK


@needs_ocr
async def test_a_wrong_amount_is_a_mismatch(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    shot = pics.screenshot(pics.payment_lines(amount="49.00", when=stamp()))
    await svc.payments.attach_evidence(U, pid, shot)
    row = await analysis(app_pool, pid)
    assert row["verdict"] == MISMATCH and "amount_mismatch" in row["findings"]
    assert row["amount_minor"] == 4900


@needs_ocr
async def test_a_different_payee_is_flagged(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    shot = pics.screenshot(pics.payment_lines(upi="another@paytm", when=stamp()))
    await svc.payments.attach_evidence(U, pid, shot)
    row = await analysis(app_pool, pid)
    assert row["verdict"] == MISMATCH and "payee_mismatch" in row["findings"]


@needs_ocr
async def test_a_failed_payment_screenshot_is_flagged(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    shot = pics.screenshot(pics.payment_lines(status="Payment Failed", when=stamp()))
    await svc.payments.attach_evidence(U, pid, shot)
    assert (await analysis(app_pool, pid))["verdict"] == MISMATCH


@needs_ocr
async def test_an_old_screenshot_is_a_warning(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    old = (datetime.now(UTC) - timedelta(days=9)).strftime("%d %b %Y, %I:%M %p")
    await svc.payments.attach_evidence(U, pid, pics.screenshot(pics.payment_lines(when=old)))
    row = await analysis(app_pool, pid)
    assert row["verdict"] == WARNING and "time_too_old" in row["findings"]


@needs_ocr
async def test_a_normal_photo_is_unreadable_but_accepted(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    assert (await svc.payments.attach_evidence(U, pid, pics.photo("JPEG"))).kind == "ok"
    row = await analysis(app_pool, pid)
    assert row["verdict"] == UNREADABLE and row["findings"] in (
        '["no_text"]',
        '["no_payment_info"]',
    )


@needs_ocr
async def test_a_blank_image_is_unreadable(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    assert (await svc.payments.attach_evidence(U, pid, pics.screenshot([]))).kind == "ok"
    assert (await analysis(app_pool, pid))["verdict"] == UNREADABLE


@needs_ocr
async def test_text_that_is_not_a_payment_is_unreadable(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    lines = [
        "Hello, this is just a long chat",
        "message about the weather today",
        "nothing else here",
    ]
    await svc.payments.attach_evidence(U, pid, pics.screenshot(lines))
    assert (await analysis(app_pool, pid))["verdict"] == UNREADABLE


@needs_ocr
async def test_a_replaced_screenshot_replaces_the_analysis(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    await svc.payments.attach_evidence(
        U, pid, pics.screenshot(pics.payment_lines(amount="49.00", when=stamp()))
    )
    assert (await analysis(app_pool, pid))["verdict"] == MISMATCH
    clock.advance(60)
    await svc.payments.attach_evidence(U, pid, pics.screenshot(pics.payment_lines(when=stamp())))
    assert (await analysis(app_pool, pid))["verdict"] == OK
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence_analysis") == 1


# ---------------------------------------------------------------- failure paths


@pytest.mark.parametrize("reason", ["timeout", "failed", "too_large", "unavailable"])
async def test_ocr_failure_is_unreadable_and_never_blocks_the_order(app_pool, clock, reason):
    svc, pid = await order(app_pool, FakeOcr(error=OcrError(reason)), clock)
    assert (await svc.payments.attach_evidence(U, pid, tiny.PNG)).kind == "ok"
    row = await analysis(app_pool, pid)
    assert row["verdict"] == UNREADABLE and row["findings"] == f'["ocr_{reason}"]'
    assert (await svc.payments.submit(U, pid, None)).kind == "ok"  # the order goes on


async def test_a_crashing_engine_still_leaves_a_stored_screenshot(app_pool, clock, caplog):
    svc, pid = await order(app_pool, FakeOcr(error=RuntimeError("boom")), clock)
    assert (await svc.payments.attach_evidence(U, pid, tiny.PNG)).kind == "ok"
    assert await analysis(app_pool, pid) is None  # not analysed
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 1


async def test_a_missing_tesseract_binary_degrades_to_unreadable(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr("no-such-ocr-binary"), clock)
    assert (await svc.payments.attach_evidence(U, pid, tiny.PNG)).kind == "ok"
    row = await analysis(app_pool, pid)
    assert row["verdict"] == UNREADABLE and row["findings"] == '["ocr_unavailable"]'


async def test_without_any_engine_the_upload_still_works(app_pool, clock):
    from bot.services.payments import PaymentService
    from bot.services.rate_limit import RateLimiter

    await enable_payments(app_pool)
    await make_user(app_pool, U)
    plan = await make_plan(app_pool)
    payments = PaymentService(app_pool, RateLimiter(clock), frozenset(), None)
    pid = (await payments.start(U, plan)).order.payment_id
    assert (await payments.attach_evidence(U, pid, tiny.PNG)).kind == "ok"
    assert (await analysis(app_pool, pid))["findings"] == '["ocr_unavailable"]'


async def test_nothing_runs_for_a_refused_image(app_pool, clock):
    ocr = FakeOcr(text="irrelevant")
    svc, pid = await order(app_pool, ocr, clock)
    for bad in (b"", b"GIF89a" + b"0" * 100, b"<svg xmlns='http://www.w3.org/2000/svg'/>" * 3):
        assert (await svc.payments.attach_evidence(U, pid, bad)).kind == "not_image"
        clock.advance(20)
    assert ocr.calls == 0 and await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0


# ---------------------------------------------------------------- validation regressions


async def test_existing_validation_still_refuses_the_bad_files(app_pool, clock):
    ocr = FakeOcr(text="")
    svc, pid = await order(app_pool, ocr, clock)
    good = tiny.PNG
    cases = {
        "corrupt": good[:-9],  # truncated
        "not_image": b"MZ" + b"\x90" * 200,  # an executable
        "too_big": good + b"0" * (3 * 1024 * 1024),
        "corrupt ": good + b"<?php system($_GET[1]); ?>",  # image with something appended
    }
    for name, data in cases.items():
        res = await svc.payments.attach_evidence(U, pid, data)
        assert res.kind == name.strip(), name
        clock.advance(20)
    assert ocr.calls == 0
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0


async def test_a_valid_image_with_a_misleading_extension_is_judged_by_content(app_pool, clock, tg):
    """The file name / MIME type the client claims never matters: a real PNG named .exe is fine,
    a script named .png is not."""
    await enable_payments(app_pool)
    await make_plan(app_pool)
    await tg.onboard(U)
    plan = await app_pool.fetchval("SELECT id FROM plans WHERE code = 'premium_30'")
    out = await tg.press(U, f"pm:s:{plan}")
    pid = int(
        [
            b.callback_data
            for o in out
            for r in (getattr(o.markup, "inline_keyboard", None) or [])
            for b in r
            if b.callback_data.startswith("pm:u:")
        ][0].split(":")[2]
    )
    await tg.press(U, f"pm:u:{pid}")
    await tg.document_bytes(U, b"#!/bin/sh\nrm -rf /\n" * 5, mime="image/png", name="shot.png")
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0
    await tg.document_bytes(U, tiny.PNG, mime="image/png", name="payment.exe", file_id="D2")
    assert await app_pool.fetchval("SELECT content_type FROM payment_evidence") == "image/png"


# ---------------------------------------------------------------- privacy of the analysis


@needs_ocr
async def test_only_extracted_fields_are_stored_never_the_text(app_pool, clock):
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    await svc.payments.attach_evidence(U, pid, pics.screenshot(pics.payment_lines(when=stamp())))
    row = await analysis(app_pool, pid)
    dumped = " ".join(str(v) for v in dict(row).values())
    for phrase in ("Payment Successful", "Paid to", "UPI Ref No", "Rs."):
        assert phrase not in dumped
    cols = await app_pool.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = 'payment_evidence_analysis'"
    )
    assert {c["column_name"] for c in cols} == {
        "payment_id", "sha256", "verdict", "engine", "amount_minor", "payee", "utr", "paid_at",
        "success_marker", "findings", "analyzed_at",
    }  # fmt: skip


@needs_ocr
async def test_extracted_payment_data_never_reaches_the_logs(app_pool, clock, caplog):
    caplog.set_level(logging.DEBUG)
    svc, pid = await order(app_pool, TesseractOcr(), clock)
    await svc.payments.attach_evidence(U, pid, pics.screenshot(pics.payment_lines(when=stamp())))
    for secret in ("412345678901", "shop@okbank", "9900", "99.00"):
        assert secret not in caplog.text
    assert "analysed" in caplog.text  # the verdict itself is logged, nothing else


async def test_the_analysis_is_deleted_with_the_screenshot(app_pool, clock):
    svc, pid = await order(
        app_pool, FakeOcr(text="Payment Successful Rs. 99.00 Paid to shop@okbank"), clock
    )
    await svc.payments.attach_evidence(U, pid, tiny.PNG)
    assert await analysis(app_pool, pid) is not None
    await svc.payments.submit(U, pid, None)
    res = await svc.payments.reject(ADMIN, pid, "not received")
    assert res.kind == "ok"
    await app_pool.execute(
        "ALTER TABLE payments DISABLE TRIGGER payments_guard_update"
    )  # time travel
    await app_pool.execute(
        "UPDATE payments SET reviewed_at = now() - interval '200 days' WHERE id = $1", pid
    )
    await app_pool.execute("ALTER TABLE payments ENABLE TRIGGER payments_guard_update")
    await svc.payments.maintain()
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence_analysis") == 0


# ---------------------------------------------------------------- advisory only


@pytest.mark.parametrize(
    "text", ["Payment Successful Rs. 99.00 Paid to shop@okbank", "Rs. 49.00 failed"]
)
async def test_the_analysis_never_approves_or_rejects_anything(app_pool, clock, text):
    svc, pid = await order(app_pool, FakeOcr(text=text), clock)
    await svc.payments.attach_evidence(U, pid, tiny.PNG)
    await svc.payments.submit(U, pid, "412345678901")
    status = await app_pool.fetchval("SELECT status FROM payments WHERE id = $1", pid)
    assert status == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    assert (await svc.payments.approve(U, pid)).kind == "denied"  # a user is no admin
    # even a MISMATCH does not stop an admin who has verified the money elsewhere
    assert (await svc.payments.approve(ADMIN, pid)).kind == "ok"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1


async def test_the_detail_view_carries_the_analysis_and_the_utr_comparison(app_pool, clock):
    text = f"Payment Successful Rs. 99.00 Paid to shop@okbank UPI Ref No: 412345678901 {stamp()}"
    svc, pid = await order(app_pool, FakeOcr(text=text), clock)
    await svc.payments.attach_evidence(U, pid, tiny.PNG)
    await svc.payments.submit(U, pid, "999999999999")
    view = (await svc.payments.detail(ADMIN, pid))["analysis"]
    assert view["verdict"] == OK and view["amount_minor"] == 9900 == view["expected_minor"]
    assert view["utr"] == "412345678901" and view["utr_check"] == "mismatch"
    assert await svc.payments.detail(U, pid) is None  # not for users
