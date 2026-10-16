"""Direct screenshot upload: the order is found from the user id, never from client data."""

import asyncio

import pytest

from bot.services import build_services
from bot.services.evidence import MISMATCH, OK, UNREADABLE
from tests import payment_images as img
from tests.factories import enable_payments, make_plan, make_user

A, B, ADMIN = 8201, 8202, 424242


class FakeOcr:
    name = "fake"

    def __init__(self, text=""):
        self.text = text

    async def read(self, data, width, height):
        return self.text


@pytest.fixture
async def shop(app_pool, clock):
    await enable_payments(app_pool)
    for uid in (A, B):
        await make_user(app_pool, uid)
    plan = await make_plan(app_pool, price_minor=9900)
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}), ocr=FakeOcr())
    return svc, plan


async def start(svc, uid, plan):
    return (await svc.payments.start(uid, plan)).order.payment_id


async def status(pool, pid):
    return await pool.fetchval("SELECT status FROM payments WHERE id = $1", pid)


async def test_a_photo_submits_the_users_own_open_order(app_pool, shop):
    svc, plan = shop
    pid = await start(svc, A, plan)
    res = await svc.payments.submit_screenshot(A, img.JPEG)
    assert (res.kind, res.payment_id) == ("ok", pid)
    assert await status(app_pool, pid) == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 1


async def test_without_an_order_nothing_is_created(app_pool, shop):
    svc, _ = shop
    assert (await svc.payments.submit_screenshot(A, img.JPEG)).kind == "no_order"
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0


async def test_evidence_can_only_land_on_the_senders_own_order(app_pool, shop):
    svc, plan = shop
    pid_a = await start(svc, A, plan)
    assert (await svc.payments.submit_screenshot(B, img.JPEG)).kind == "no_order"
    assert await status(app_pool, pid_a) == "created"
    pid_b = await start(svc, B, plan)
    assert (await svc.payments.submit_screenshot(B, img.PNG)).payment_id == pid_b
    assert await status(app_pool, pid_a) == "created"
    owners = await app_pool.fetch(
        "SELECT p.user_id FROM payment_evidence e JOIN payments p ON p.id = e.payment_id"
    )
    assert [r["user_id"] for r in owners] == [B]


async def test_the_api_takes_no_payment_id_so_none_can_be_spoofed():
    import inspect

    from bot.services.payments import PaymentService

    assert list(inspect.signature(PaymentService.submit_screenshot).parameters) == [
        "self",
        "uid",
        "data",
        "utr_raw",
    ]
    assert list(inspect.signature(PaymentService.add_utr).parameters) == ["self", "uid", "utr_raw"]


async def test_a_second_photo_is_refused(app_pool, shop, clock):
    svc, plan = shop
    pid = await start(svc, A, plan)
    await svc.payments.submit_screenshot(A, img.JPEG)
    clock.advance(60)
    res = await svc.payments.submit_screenshot(A, img.PNG)
    assert res.kind == "already_submitted" and res.payment_id == pid
    assert await app_pool.fetchval("SELECT content_type FROM payment_evidence") == "image/jpeg"


async def test_concurrent_photos_and_utrs_leave_one_consistent_submission(app_pool, shop, clock):
    svc, plan = shop
    pid = await start(svc, A, plan)
    clock.advance(1000)
    runs = await asyncio.gather(*(svc.payments.submit_screenshot(A, img.JPEG) for _ in range(8)))
    assert [r.kind for r in runs].count("ok") == 1
    utrs = await asyncio.gather(*(svc.payments.add_utr(A, f"UTR00000{i}00") for i in range(5)))
    assert [r.kind for r in utrs].count("ok") <= 1
    assert await status(app_pool, pid) == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 1


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"", "not_image"),
        (b"GIF89a" + b"0" * 100, "not_image"),
        (img.JPEG[:-30], "corrupt"),
        (img.PNG + b"<?php ?>", "corrupt"),
        (img.JPEG + b"0" * (3 * 1024 * 1024), "too_big"),
    ],
)
async def test_invalid_images_are_refused_and_the_order_stays_open(app_pool, shop, data, kind):
    svc, plan = shop
    pid = await start(svc, A, plan)
    assert (await svc.payments.submit_screenshot(A, data)).kind == kind
    assert await status(app_pool, pid) == "created"
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0


async def test_the_utr_is_optional_and_can_come_from_a_caption(app_pool, shop):
    svc, plan = shop
    pid = await start(svc, A, plan)
    res = await svc.payments.submit_screenshot(A, img.JPEG, "UPI Ref No: 4123 4567 8901")
    assert res.kind == "ok" and res.reason is None
    assert await app_pool.fetchval("SELECT utr FROM payments WHERE id=$1", pid) == "412345678901"


async def test_a_nonsense_caption_is_ignored_not_fatal(app_pool, shop):
    svc, plan = shop
    pid = await start(svc, A, plan)
    res = await svc.payments.submit_screenshot(A, img.JPEG, "here you go!!")
    assert (res.kind, res.reason) == ("ok", "utr_ignored")
    assert await app_pool.fetchval("SELECT utr FROM payments WHERE id=$1", pid) is None
    assert (await svc.payments.add_utr(A, "UTR000000011")).kind == "ok"
    assert (await svc.payments.add_utr(A, "UTR000000012")).kind == "utr_already"


async def test_add_utr_needs_a_submitted_order_of_your_own(app_pool, shop):
    svc, plan = shop
    assert (await svc.payments.add_utr(A, "UTR000000011")).kind == "no_order"
    await start(svc, A, plan)
    assert (await svc.payments.add_utr(A, "UTR000000011")).kind == "invalid_state"  # not submitted
    await svc.payments.submit_screenshot(A, img.JPEG)
    assert (await svc.payments.add_utr(B, "UTR000000011")).kind == "no_order"
    assert (await svc.payments.add_utr(A, "no!")).kind == "invalid_utr"


async def test_a_utr_already_pending_elsewhere_is_refused(app_pool, shop):
    svc, plan = shop
    await start(svc, A, plan)
    await svc.payments.submit_screenshot(A, img.JPEG, "UTR000000011")
    pid_b = await start(svc, B, plan)
    assert (
        await svc.payments.submit_screenshot(B, img.PNG, "UTR000000011")
    ).kind == "duplicate_utr"
    assert await status(app_pool, pid_b) == "created"
    await svc.payments.submit_screenshot(B, img.PNG)
    assert (await svc.payments.add_utr(B, "utr000000011")).kind == "duplicate_utr"


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("Payment Successful Rs. 99.00 Paid to shop@okbank UPI Ref No: 412345678901", OK),
        ("Payment Successful Rs. 49.00 Paid to shop@okbank", MISMATCH),
        ("", UNREADABLE),
    ],
)
async def test_ocr_is_advisory_whatever_it_says_the_order_waits_for_an_admin(
    app_pool, clock, text, verdict
):
    await enable_payments(app_pool)
    await make_user(app_pool, A)
    plan = await make_plan(app_pool, price_minor=9900)
    svc = build_services(app_pool, clock, clock, frozenset({ADMIN}), ocr=FakeOcr(text))
    pid = await start(svc, A, plan)
    await svc.payments.submit_screenshot(A, img.JPEG)
    got = await app_pool.fetchval(
        "SELECT verdict FROM payment_evidence_analysis WHERE payment_id=$1", pid
    )
    assert got in (verdict, "VALIDATION_WARNING") and (verdict != OK or got != UNREADABLE)
    assert await status(app_pool, pid) == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0


async def test_rejection_and_approval_still_belong_to_the_admin(app_pool, shop):
    svc, plan = shop
    pid = await start(svc, A, plan)
    await svc.payments.submit_screenshot(A, img.JPEG)
    assert (await svc.payments.approve(A, pid)).kind == "denied"
    assert (await svc.payments.reject(ADMIN, pid, "no money received")).kind == "ok"
    assert (await svc.payments.approve(ADMIN, pid)).kind == "already_handled"
    # a rejected order is final: a new photo has no order to attach to
    assert (await svc.payments.submit_screenshot(A, img.PNG)).kind == "no_order"
    pid2 = await start(svc, A, plan)
    await svc.payments.submit_screenshot(A, img.PNG)
    results = await asyncio.gather(*(svc.payments.approve(ADMIN, pid2) for _ in range(6)))
    assert [r.kind for r in results].count("ok") == 1
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1


async def test_cancelled_and_expired_orders_take_no_photos(app_pool, shop):
    svc, plan = shop
    pid = await start(svc, A, plan)
    assert (await svc.payments.cancel(A, pid)).kind == "ok"
    assert (await svc.payments.submit_screenshot(A, img.JPEG)).kind == "no_order"
    pid = await start(svc, A, plan)
    await app_pool.execute(
        "UPDATE payments SET created_at = now() - interval '25 hours' WHERE status = 'created'"
    )
    await svc.payments.maintain()
    assert await status(app_pool, pid) == "cancelled"
    assert (await svc.payments.submit_screenshot(A, img.JPEG)).kind == "no_order"
