"""The admin payment pages show the advisory screenshot analysis, clearly and safely."""

from datetime import datetime

import pytest

from bot.db import repo_payments
from bot.services.evidence import MISMATCH, OK, UNREADABLE, WARNING, Analysis
from tests import payment_images as img
from tests.factories import enable_payments, make_plan, make_user


@pytest.fixture
async def shop(admin):
    await enable_payments(admin.pool)
    plan = await make_plan(admin.pool)
    for uid in (1, 2):
        await make_user(admin.pool, uid)
    return plan


async def submitted(admin, uid, plan, analysis, *, utr=None):
    svc = admin.state.services.payments
    pid = (await svc.start(uid, plan)).order.payment_id
    await svc.attach_evidence(uid, pid, img.JPEG)
    sha = await admin.pool.fetchval("SELECT sha256 FROM payment_evidence WHERE payment_id=$1", pid)
    if analysis is not None:
        assert await repo_payments.put_analysis(admin.pool, pid, sha, analysis)
    else:  # whatever the real engine concluded about the tiny test image: remove it
        await admin.pool.execute("DELETE FROM payment_evidence_analysis")
    await svc.submit(uid, pid, utr)
    return pid


async def page(admin, pid):
    r = await admin.client.get(f"/admin/payments/{pid}")
    assert r.status_code == 200
    return r.text


async def test_an_ok_analysis_shows_the_extracted_fields(admin, shop):
    a = Analysis(
        OK, "tesseract", 9900, "shop@okbank", "412345678901", datetime(2026, 10, 16, 11, 58), True
    )
    pid = await submitted(admin, 1, shop, a, utr="412345678901")
    html = await page(admin, pid)
    for needle in (
        "Screenshot check",
        "advisory only",
        "Looks consistent",
        "VALIDATION_OK",
        "₹99",
        "(matches the order)",
        "shop@okbank",
        "412345678901",
        "(same as typed by user)",
        "16-10-2026 11:58 IST",
        "Always confirm the payment in your own bank",
    ):
        assert needle in html


async def test_a_mismatch_lists_its_warnings_in_plain_words(admin, shop):
    a = Analysis(MISMATCH, "tesseract", 4900, "other@paytm", "999999999999", None, True,
                 ("amount_mismatch", "payee_mismatch"))  # fmt: skip
    pid = await submitted(admin, 1, shop, a, utr="412345678901")
    html = await page(admin, pid)
    assert "Does not match the order" in html
    assert "The amount on the screenshot is different from the order." in html
    assert "A different UPI ID than the one on the order is shown." in html
    assert "(order: ₹99)" in html and "other@paytm" in html
    assert "different from the user" in html  # the UTR disagrees with what the user typed


async def test_warning_and_unreadable_are_distinct(admin, shop):
    w = await submitted(
        admin, 1, shop, Analysis(WARNING, "t", 9900, None, None, None, None, ("payee_not_found",))
    )
    u = await submitted(admin, 2, shop, Analysis(UNREADABLE, "t", findings=("no_text",)))
    assert "Check carefully" in await page(admin, w)
    html = await page(admin, u)
    assert "Could not be read" in html and "Almost no text could be read" in html
    assert "— not read —" in html


async def test_a_payment_without_an_analysis_says_so(admin, shop):
    pid = await submitted(admin, 1, shop, None)
    assert "Not analysed" in await page(admin, pid)


async def test_the_list_shows_the_verdict_per_payment(admin, shop):
    await submitted(admin, 1, shop, Analysis(MISMATCH, "t", findings=("amount_mismatch",)))
    await submitted(admin, 2, shop, Analysis(OK, "t", 9900, None, None, None, True))
    html = (await admin.client.get("/admin/payments")).text
    assert "Screenshot check" in html
    assert "Does not match the order" in html and "Looks consistent" in html


async def test_unknown_finding_codes_are_escaped_not_trusted(admin, shop):
    a = Analysis(WARNING, "t", findings=("<script>alert(1)</script>",))
    pid = await submitted(admin, 1, shop, a)
    html = await page(admin, pid)
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


async def test_the_analysis_is_admin_only(web, shop):
    r = await web.client.get("/admin/payments/1")
    assert r.status_code in (302, 303, 401, 403, 404)  # the panel does not reveal itself
    assert "Screenshot check" not in r.text
