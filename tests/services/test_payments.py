"""P6 manual payments: configuration, plans, orders, evidence, UTR, states, approval, expiry."""

import asyncio

import asyncpg
import pytest

from bot.services import build_services
from bot.services.payments import normalize_utr
from bot.services.upi import format_inr, parse_rupees, upi_uri
from tests import payment_images as img
from tests.factories import enable_payments, make_plan, make_user

ADMIN = 9000001
OTHER_ADMIN = 9000002
ADMINS = frozenset({ADMIN, OTHER_ADMIN})


@pytest.fixture
def psvc(app_pool, clock):
    return build_services(app_pool, clock, clock, ADMINS)


@pytest.fixture
async def shop(app_pool, psvc):
    """Payments switched on, one purchasable plan (Rs 99 / 30 days), users 1 and 2 active."""
    await enable_payments(app_pool)
    plan = await make_plan(app_pool)
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    return plan


async def order_and_submit(psvc, uid, plan, utr=None, image=img.JPEG):
    started = await psvc.payments.start(uid, plan)
    assert started.kind == "ok", started
    pid = started.order.payment_id
    assert (await psvc.payments.attach_evidence(uid, pid, image)).kind == "ok"
    res = await psvc.payments.submit(uid, pid, utr)
    assert res.kind == "ok", res
    return pid


async def audit(pool):
    return [dict(r) for r in await pool.fetch("SELECT * FROM admin_actions ORDER BY id")]


# ------------------------------------------------------------------ A. configuration


async def test_admin_reads_and_updates_the_payment_configuration(app_pool, psvc):
    s = await psvc.payments.get_settings(ADMIN)
    assert (s["is_active"], s["upi_id"]) == (False, None)  # shipped switched off
    res = await psvc.payments.save_settings(
        ADMIN,
        upi_id=" me@okhdfc ",
        payee_name="  Xenora  Meet ",
        instructions="Line1\n\n  Line2 ",
        active=True,
    )
    assert res.kind == "ok"
    s = await psvc.payments.get_settings(OTHER_ADMIN)
    assert (s["upi_id"], s["payee_name"], s["instructions"], s["is_active"]) == (
        "me@okhdfc",
        "Xenora Meet",
        "Line1\n\nLine2",
        True,
    )
    assert s["updated_by"] == ADMIN
    rows = await audit(app_pool)
    assert [r["action"] for r in rows] == ["payment_config"]
    assert "me@okhdfc" not in (rows[0]["detail"] or "")  # no payment details in the audit log


async def test_non_admin_cannot_read_or_change_the_configuration(app_pool, psvc):
    assert await psvc.payments.get_settings(5) is None
    res = await psvc.payments.save_settings(
        5, upi_id="a@b", payee_name="x", instructions="", active=True
    )
    assert res.kind == "denied"
    assert (await app_pool.fetchrow("SELECT upi_id, is_active FROM payment_settings"))[
        "upi_id"
    ] is None
    assert await audit(app_pool) == []


@pytest.mark.parametrize(
    "upi,payee,active,field",
    [
        ("not a upi", "X", True, "upi_id"),
        ("a@b", "X", True, "upi_id"),  # handle must start with a letter and be 2+ chars
        ("x@bank'; DROP", "X", False, "upi_id"),
        ("", "X", True, "upi_id"),  # cannot switch on without a UPI id
        ("ok@bank", "", True, "payee_name"),
    ],
)
async def test_invalid_configuration_is_refused(psvc, app_pool, upi, payee, active, field):
    res = await psvc.payments.save_settings(
        ADMIN, upi_id=upi, payee_name=payee, instructions="", active=active
    )
    assert (res.kind, res.field) == ("invalid", field)
    assert (await app_pool.fetchrow("SELECT upi_id FROM payment_settings"))["upi_id"] is None


async def test_the_configuration_table_can_only_ever_have_one_row(app_pool):
    with pytest.raises(asyncpg.PostgresError):
        await app_pool.execute("INSERT INTO payment_settings (id) VALUES (false)")
    with pytest.raises(asyncpg.PostgresError):
        await app_pool.execute("UPDATE payment_settings SET is_active = true")  # needs upi + payee


# ------------------------------------------------------------------ B. plans


async def test_admin_creates_and_edits_plans_without_touching_the_free_plan(app_pool, psvc):
    res = await psvc.payments.save_plan(
        ADMIN, None, code="premium_30", name="Premium 30 Days", days="30", price="99", active=True
    )
    assert res.kind == "ok"
    plan = await app_pool.fetchrow("SELECT * FROM plans WHERE code='premium_30'")
    assert (plan["price_minor"], plan["duration_days"], plan["currency"]) == (9900, 30, "INR")
    feats = await app_pool.fetchval(
        "SELECT count(*) FROM plan_entitlements WHERE plan_id=$1", plan["id"]
    )
    assert feats == 3  # same feature flags as the free plan: paid never offers less
    res = await psvc.payments.save_plan(
        ADMIN, plan["id"], code="", name="Premium", days="31", price="149.50", active=False
    )
    assert res.kind == "ok"
    plan = await app_pool.fetchrow("SELECT * FROM plans WHERE code='premium_30'")
    assert (plan["name"], plan["price_minor"], plan["duration_days"], plan["is_active"]) == (
        "Premium",
        14950,
        31,
        False,
    )
    free = await app_pool.fetchval("SELECT id FROM plans WHERE code='free'")
    assert (
        await psvc.payments.save_plan(
            ADMIN, free, code="", name="x", days="1", price="1", active=True
        )
    ).kind == "not_found"
    assert await app_pool.fetchval("SELECT price_minor FROM plans WHERE code='free'") is None
    assert [r["action"] for r in await audit(app_pool)] == ["plan_save", "plan_save"]


@pytest.mark.parametrize(
    "kw,field",
    [
        ({"code": "Bad Code"}, "code"),
        ({"code": "free"}, "code"),
        ({"code": ""}, "code"),
        ({"name": "  "}, "name"),
        ({"days": "0"}, "days"),
        ({"days": "3661"}, "days"),
        ({"days": "ten"}, "days"),
        ({"price": "0"}, "price"),
        ({"price": "-5"}, "price"),
        ({"price": "9.999"}, "price"),
        ({"price": "1e3"}, "price"),
        ({"price": "99999999"}, "price"),
    ],
)
async def test_invalid_plan_input_is_refused(psvc, app_pool, kw, field):
    args = {"code": "p1", "name": "Plan", "days": "30", "price": "99", "active": True} | kw
    res = await psvc.payments.save_plan(ADMIN, None, **args)
    assert (res.kind, res.field) == ("invalid", field)
    assert await app_pool.fetchval("SELECT count(*) FROM plans") == 1


async def test_duplicate_plan_code_and_non_admin_are_refused(psvc):
    ok = dict(code="p1", name="Plan", days="30", price="99", active=True)
    assert (await psvc.payments.save_plan(ADMIN, None, **ok)).kind == "ok"
    assert (await psvc.payments.save_plan(ADMIN, None, **ok)).kind == "exists"
    assert (await psvc.payments.save_plan(7, None, **{**ok, "code": "p2"})).kind == "denied"
    assert await psvc.payments.plans(7) == []


async def test_only_active_priced_plans_can_be_bought(app_pool, psvc, shop):
    inactive = await make_plan(app_pool, "old", price_minor=5000, active=False)
    free = await app_pool.fetchval("SELECT id FROM plans WHERE code='free'")
    assert [p["code"] for p in (await psvc.payments.overview(1)).plans] == ["premium_30"]
    for plan in (inactive, free, 99999):
        assert (await psvc.payments.start(1, plan)).kind == "plan_unavailable"
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 0


# ------------------------------------------------------------------ C. orders


async def test_order_stores_the_exact_plan_amount_and_a_unique_reference(app_pool, psvc, shop):
    a = await psvc.payments.start(1, shop)
    b = await psvc.payments.start(2, shop)
    assert a.kind == b.kind == "ok"
    row = await app_pool.fetchrow("SELECT * FROM payments WHERE id=$1", a.order.payment_id)
    assert (row["user_id"], row["plan_id"], row["amount_minor"], row["currency"]) == (
        1,
        shop,
        9900,
        "INR",
    )
    assert (row["duration_days"], row["status"], row["pay_to"]) == (30, "created", "shop@okbank")
    assert a.order.reference != b.order.reference
    assert a.order.reference.startswith("PAY-") and len(a.order.reference) == 14
    assert a.order.amount == "99" and a.order.payee_name == "Test Payee"
    assert (
        "am=99.00" in a.order.uri and "cu=INR" in a.order.uri and a.order.reference in a.order.uri
    )


async def test_starting_again_reuses_the_open_order_and_other_plans_replace_it(
    app_pool, psvc, shop
):
    first = await psvc.payments.start(1, shop)
    again = await psvc.payments.start(1, shop)
    assert again.order.payment_id == first.order.payment_id
    other = await make_plan(app_pool, "premium_90", name="Premium 90", days=90, price_minor=24900)
    switched = await psvc.payments.start(1, other)
    assert switched.order.payment_id != first.order.payment_id and switched.order.amount == "249"
    statuses = {
        r["id"]: r["status"] for r in await app_pool.fetch("SELECT id, status FROM payments")
    }
    assert statuses == {first.order.payment_id: "cancelled", switched.order.payment_id: "created"}


async def test_a_price_change_gives_a_fresh_order_but_old_orders_keep_their_amount(
    app_pool, psvc, shop
):
    first = await psvc.payments.start(1, shop)
    await app_pool.execute("UPDATE plans SET price_minor = 12900 WHERE id=$1", shop)
    second = await psvc.payments.start(1, shop)
    assert second.order.payment_id != first.order.payment_id and second.order.amount == "129"
    assert (
        await app_pool.fetchval(
            "SELECT amount_minor FROM payments WHERE id=$1", first.order.payment_id
        )
        == 9900
    )


async def test_payments_must_be_configured_and_switched_on(app_pool, psvc):
    plan = await make_plan(app_pool)
    await make_user(app_pool, 1)
    assert (await psvc.payments.start(1, plan)).kind == "unavailable"
    assert not (await psvc.payments.overview(1)).ready
    await enable_payments(app_pool)
    await app_pool.execute("UPDATE payment_settings SET is_active = false")
    assert (await psvc.payments.start(1, plan)).kind == "unavailable"


async def test_only_active_adults_can_open_an_order(app_pool, psvc, shop):
    await make_user(app_pool, 10, status="onboarding")
    await make_user(app_pool, 11, status="banned")
    for uid in (10, 11, 404):
        assert (await psvc.payments.start(uid, shop)).kind == "not_active"


async def test_a_user_has_at_most_one_open_order_and_cannot_flood(app_pool, psvc, shop, clock):
    pid = await order_and_submit(psvc, 1, shop)
    res = await psvc.payments.start(1, shop)
    assert res.kind == "has_pending" and res.payment["id"] == pid
    with pytest.raises(asyncpg.UniqueViolationError):  # the database enforces it too
        await app_pool.execute(
            "INSERT INTO payments (reference, user_id, plan_id, amount_minor, currency, "
            "duration_days, pay_to) VALUES ('PAY-AAAAAAAAAA', 1, $1, 100, 'INR', 1, 'x@y')",
            shop,
        )
    other = await make_plan(app_pool, "p2", price_minor=100)
    kinds = [(await psvc.payments.start(2, shop if n % 2 else other)).kind for n in range(10)]
    assert "rate_limited" in kinds


# ------------------------------------------------------------------ D. screenshots


async def test_a_valid_screenshot_is_stored_as_evidence_only(app_pool, psvc, shop):
    order = (await psvc.payments.start(1, shop)).order
    res = await psvc.payments.attach_evidence(1, order.payment_id, img.PNG)
    assert res.kind == "ok"
    ev = await app_pool.fetchrow("SELECT * FROM payment_evidence")
    assert (ev["content_type"], ev["size_bytes"], bytes(ev["data"])) == (
        "image/png",
        len(img.PNG),
        img.PNG,
    )
    assert len(ev["sha256"]) == 64
    assert (
        await app_pool.fetchval("SELECT status FROM payments") == "created"
    )  # NOT approved, NOT even pending
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0


async def test_a_new_screenshot_replaces_the_old_one_before_submission(app_pool, psvc, shop):
    order = (await psvc.payments.start(1, shop)).order
    await psvc.payments.attach_evidence(1, order.payment_id, img.PNG)
    await psvc.payments.attach_evidence(1, order.payment_id, img.JPEG)
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 1
    assert await app_pool.fetchval("SELECT content_type FROM payment_evidence") == "image/jpeg"


@pytest.mark.parametrize(
    "data,kind",
    [
        (b"<?php system($_GET['c']); ?>" * 5, "not_image"),
        (b"<svg onload=alert(1)></svg>" * 4, "not_image"),
        (b"GIF89a" + b"\x00" * 60, "not_image"),
        (img.JPEG[:-30], "corrupt"),
        (img.PNG + b"<?php ?>", "corrupt"),
        (img.JPEG + b"\x00" * (3 * 1024 * 1024), "too_big"),
    ],
)
async def test_dangerous_invalid_or_oversized_uploads_are_rejected_and_not_stored(
    app_pool, psvc, shop, data, kind
):
    order = (await psvc.payments.start(1, shop)).order
    res = await psvc.payments.attach_evidence(1, order.payment_id, data)
    assert res.kind == kind
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0


async def test_evidence_is_bytes_in_the_database_never_a_path_or_a_user_file_name(app_pool):
    cols = {
        r["column_name"]
        for r in await app_pool.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'payment_evidence'"
        )
    }
    assert cols == {"payment_id", "content_type", "size_bytes", "sha256", "data", "created_at"}


async def test_nobody_can_attach_evidence_to_someone_elses_or_a_submitted_order(
    app_pool, psvc, shop
):
    order = (await psvc.payments.start(1, shop)).order
    assert (
        await psvc.payments.attach_evidence(2, order.payment_id, img.JPEG)
    ).kind == "invalid_state"
    assert (await psvc.payments.attach_evidence(1, 424242, img.JPEG)).kind == "invalid_state"
    await psvc.payments.attach_evidence(1, order.payment_id, img.JPEG)
    await psvc.payments.submit(1, order.payment_id, None)
    assert (
        await psvc.payments.attach_evidence(1, order.payment_id, img.PNG)
    ).kind == "invalid_state"


# ------------------------------------------------------------------ E. UTR


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("412345678901", "412345678901"),
        (" ab12 cd34-ef56 ", "AB12CD34EF56"),
        ("t2410161230abcdefg", "T2410161230ABCDEFG"),
    ],
)
def test_utr_is_normalised(raw, expected):
    assert normalize_utr(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "12345",
        "x" * 36,
        "UTR#12345678",
        "१२३४५६७८९०१२",
        "12345678\n9",
        "<b>123456</b>",
        "12345678'; --",
    ],
)
def test_malformed_utr_is_rejected(raw):
    with pytest.raises(ValueError):
        normalize_utr(raw)


async def test_utr_is_optional_validated_and_stored_upper_case(app_pool, psvc, shop):
    order = (await psvc.payments.start(1, shop)).order
    await psvc.payments.attach_evidence(1, order.payment_id, img.JPEG)
    assert (await psvc.payments.submit(1, order.payment_id, "bad!")).kind == "invalid_utr"
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"
    assert (await psvc.payments.submit(1, order.payment_id, "ab12345678cd")).kind == "ok"
    assert await app_pool.fetchval("SELECT utr FROM payments") == "AB12345678CD"
    pid2 = await order_and_submit(psvc, 2, shop, utr=None)
    assert await app_pool.fetchval("SELECT utr FROM payments WHERE id=$1", pid2) is None


async def test_a_utr_cannot_be_reused_while_pending_or_after_approval_but_can_after_rejection(
    app_pool, psvc, shop
):
    first = await order_and_submit(psvc, 1, shop, utr="UTR000000001")
    order2 = (await psvc.payments.start(2, shop)).order
    await psvc.payments.attach_evidence(2, order2.payment_id, img.PNG)
    assert (
        await psvc.payments.submit(2, order2.payment_id, "utr000000001")
    ).kind == "duplicate_utr"
    assert await psvc.payments.approve(ADMIN, first) is not None
    assert (
        await psvc.payments.submit(2, order2.payment_id, "UTR000000001")
    ).kind == "duplicate_utr"
    # a rejected payment's UTR was not valid proof, so it may be offered again
    pid = await order_and_submit(psvc, 3, shop, utr="UTR000000002")
    await psvc.payments.reject(ADMIN, pid, "no such transaction")
    assert (await psvc.payments.submit(2, order2.payment_id, "UTR000000002")).kind == "ok"


async def test_two_pending_payments_with_one_utr_cannot_both_be_approved(app_pool, psvc, shop):
    a = await order_and_submit(psvc, 1, shop, utr="DUPLICATE0001")
    b = await order_and_submit(psvc, 2, shop)
    await app_pool.execute(
        "UPDATE payments SET utr = 'DUPLICATE0001' WHERE id = $1", b
    )  # slipped past the app check
    assert (await psvc.payments.approve(ADMIN, a)).kind == "ok"
    res = await psvc.payments.approve(ADMIN, b)
    assert res.kind == "duplicate_utr"
    assert await app_pool.fetchval("SELECT status FROM payments WHERE id=$1", b) == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions WHERE user_id=2") == 0


# ------------------------------------------------------------------ F. states


async def test_submit_needs_a_screenshot_and_an_open_order(app_pool, psvc, shop):
    order = (await psvc.payments.start(1, shop)).order
    assert (await psvc.payments.submit(1, order.payment_id, "UTR123456789")).kind == "no_evidence"
    assert (await psvc.payments.submit(2, order.payment_id, None)).kind == "invalid_state"
    await psvc.payments.attach_evidence(1, order.payment_id, img.JPEG)
    assert (await psvc.payments.submit(1, order.payment_id, None)).kind == "ok"
    assert (await psvc.payments.submit(1, order.payment_id, None)).kind == "invalid_state"


async def test_created_orders_can_be_cancelled_but_pending_ones_only_by_an_admin(
    app_pool, psvc, shop
):
    order = (await psvc.payments.start(1, shop)).order
    assert (await psvc.payments.cancel(2, order.payment_id)).kind == "not_found"  # not theirs
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"
    assert (await psvc.payments.cancel(1, order.payment_id)).kind == "ok"
    assert (await psvc.payments.cancel(1, order.payment_id)).kind == "invalid_state"
    pid = await order_and_submit(psvc, 2, shop)
    assert (await psvc.payments.cancel(2, pid)).kind == "invalid_state"
    assert await app_pool.fetchval("SELECT status FROM payments WHERE id=$1", pid) == "pending"


async def test_approved_and_rejected_payments_are_final(app_pool, psvc, shop):
    a = await order_and_submit(psvc, 1, shop)
    b = await order_and_submit(psvc, 2, shop)
    assert (await psvc.payments.approve(ADMIN, a)).kind == "ok"
    assert (await psvc.payments.reject(ADMIN, b, "screenshot is not a payment")).kind == "ok"
    assert (await psvc.payments.approve(ADMIN, a)).kind == "already_handled"
    assert (await psvc.payments.reject(ADMIN, a, "changed my mind")).kind == "already_handled"
    assert (await psvc.payments.approve(ADMIN, b)).kind == "already_handled"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1


@pytest.mark.parametrize(
    "setup,sql",
    [
        ("approved", "UPDATE payments SET status='pending'"),
        ("approved", "UPDATE payments SET amount_minor = 1"),
        ("rejected", "UPDATE payments SET status='approved'"),
        ("created", "UPDATE payments SET status='approved'"),
        ("created", "UPDATE payments SET status='rejected'"),
        ("pending", "UPDATE payments SET status='created'"),
        ("pending", "UPDATE payments SET amount_minor = 1"),
        ("pending", "UPDATE payments SET user_id = 2"),
        ("pending", "UPDATE payments SET pay_to = 'evil@bank'"),
        ("cancelled", "UPDATE payments SET status='pending'"),
    ],
)
async def test_the_database_itself_refuses_illegal_transitions_and_edits(
    app_pool, psvc, shop, setup, sql
):
    if setup == "created":
        await psvc.payments.start(1, shop)
    elif setup == "cancelled":
        order = (await psvc.payments.start(1, shop)).order
        await psvc.payments.cancel(1, order.payment_id)
    else:
        pid = await order_and_submit(psvc, 1, shop)
        if setup == "approved":
            await psvc.payments.approve(ADMIN, pid)
        elif setup == "rejected":
            await psvc.payments.reject(ADMIN, pid, "not paid")
    with pytest.raises(asyncpg.PostgresError):
        await app_pool.execute(sql)


async def test_an_approved_payment_row_must_name_its_reviewer_and_subscription(
    app_pool, psvc, shop
):
    pid = await order_and_submit(psvc, 1, shop)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("UPDATE payments SET status='approved' WHERE id=$1", pid)
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("UPDATE payments SET status='rejected' WHERE id=$1", pid)


# ------------------------------------------------------------------ G. activation


async def test_submitting_a_screenshot_does_not_activate_premium(app_pool, psvc, shop):
    await order_and_submit(psvc, 1, shop, utr="UTR000000009")
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    assert (await psvc.payments.overview(1)).premium_until is None
    assert (await psvc.entitlements.get(1, "random_chat")).source == "free"
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"


async def test_only_admin_approval_activates_the_plan(app_pool, psvc, shop):
    pid = await order_and_submit(psvc, 1, shop)
    assert (await psvc.payments.approve(5, pid)).kind == "denied"  # not an admin
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    res = await psvc.payments.approve(ADMIN, pid)
    assert res.kind == "ok" and res.user_id == 1 and res.period_end is not None
    sub = await app_pool.fetchrow("SELECT * FROM subscriptions")
    assert (sub["user_id"], sub["plan_id"], sub["status"], sub["provider"]) == (
        1,
        shop,
        "active",
        "manual_upi",
    )
    assert sub["provider_ref"] == res.reference
    days = await app_pool.fetchval(
        "SELECT extract(day from current_period_end - now()) FROM subscriptions"
    )
    assert days in (29, 30)
    pay = await app_pool.fetchrow("SELECT * FROM payments")
    assert (pay["status"], pay["reviewed_by"], pay["subscription_id"]) == (
        "approved",
        ADMIN,
        sub["id"],
    )
    assert pay["reviewed_at"] is not None
    assert (await psvc.entitlements.get(1, "random_chat")).source == "subscription"
    assert (await psvc.payments.overview(1)).premium_until == sub["current_period_end"]
    assert (await psvc.entitlements.get(2, "random_chat")).source == "free"  # others unaffected
    rows = await audit(app_pool)
    assert [(r["action"], r["admin_id"], r["target_user_id"]) for r in rows] == [
        ("payment_approve", ADMIN, 1)
    ]
    assert res.reference in rows[0]["detail"]


async def test_approval_activates_the_plan_that_was_ordered(app_pool, psvc, shop):
    big = await make_plan(app_pool, "premium_365", name="Year", days=365, price_minor=49900)
    pid = await order_and_submit(psvc, 1, big)
    await psvc.payments.approve(ADMIN, pid)
    assert await app_pool.fetchval("SELECT plan_id FROM subscriptions") == big
    days = await app_pool.fetchval(
        "SELECT extract(day from current_period_end - now()) FROM subscriptions"
    )
    assert 364 <= days <= 365


async def test_a_renewal_extends_the_live_subscription_instead_of_overlapping(app_pool, psvc, shop):
    first = await order_and_submit(psvc, 1, shop)
    await psvc.payments.approve(ADMIN, first)
    end1 = await app_pool.fetchval("SELECT current_period_end FROM subscriptions")
    second = await order_and_submit(psvc, 1, shop)
    await psvc.payments.approve(OTHER_ADMIN, second)
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    end2 = await app_pool.fetchval("SELECT current_period_end FROM subscriptions")
    assert (end2 - end1).days == 30
    subs = {
        r["subscription_id"] for r in await app_pool.fetch("SELECT subscription_id FROM payments")
    }
    assert len(subs) == 1


async def test_approval_is_refused_for_inactive_users_and_inactive_plans(app_pool, psvc, shop):
    pid = await order_and_submit(psvc, 1, shop)
    await app_pool.execute("UPDATE users SET status='banned' WHERE id=1")
    assert (await psvc.payments.approve(ADMIN, pid)).kind == "user_inactive"
    await app_pool.execute("UPDATE users SET status='active' WHERE id=1")
    await app_pool.execute("UPDATE plans SET is_active=false WHERE id=$1", shop)
    assert (await psvc.payments.approve(ADMIN, pid)).kind == "plan_inactive"
    await app_pool.execute("UPDATE plans SET is_active=true WHERE id=$1", shop)
    await app_pool.execute("DELETE FROM payment_evidence")
    assert (await psvc.payments.approve(ADMIN, pid)).kind == "no_evidence"
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    assert await audit(app_pool) == []


async def test_a_failure_halfway_through_approval_leaves_nothing_behind(
    app_pool, psvc, shop, monkeypatch
):
    pid = await order_and_submit(psvc, 1, shop)
    from bot.db import repo_payments

    async def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(repo_payments, "audit", boom)  # last step of the transaction
    with pytest.raises(RuntimeError):
        await psvc.payments.approve(ADMIN, pid)
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0


# ------------------------------------------------------------------ H. rejection


async def test_rejection_needs_a_reason_and_never_activates_premium(app_pool, psvc, shop):
    pid = await order_and_submit(psvc, 1, shop)
    for reason in (None, "", "  ", "x", "ab"):
        assert (await psvc.payments.reject(ADMIN, pid, reason)).kind == "reason_required"
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"
    res = await psvc.payments.reject(ADMIN, pid, "  Amount\x00 is   short\n by Rs 20 ")
    assert res.kind == "ok" and res.reason == "Amount is short by Rs 20"
    pay = await app_pool.fetchrow("SELECT * FROM payments")
    assert (pay["status"], pay["reviewed_by"], pay["rejection_reason"]) == (
        "rejected",
        ADMIN,
        "Amount is short by Rs 20",
    )
    assert pay["reviewed_at"] is not None and pay["subscription_id"] is None
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    assert (await psvc.entitlements.get(1, "random_chat")).source == "free"
    assert [r["action"] for r in await audit(app_pool)] == ["payment_reject"]
    assert (await psvc.payments.reject(5, pid, "whatever")).kind == "denied"


async def test_a_rejected_user_can_pay_again(app_pool, psvc, shop):
    pid = await order_and_submit(psvc, 1, shop)
    await psvc.payments.reject(ADMIN, pid, "payment not received")
    overview = await psvc.payments.overview(1)
    assert (
        overview.open_payment is None
        and overview.last_rejected["rejection_reason"] == "payment not received"
    )
    again = await psvc.payments.start(1, shop)
    assert again.kind == "ok" and again.order.payment_id != pid


# ------------------------------------------------------------------ I. concurrency


async def test_parallel_approvals_create_exactly_one_subscription(app_pool, psvc, shop):
    pid = await order_and_submit(psvc, 1, shop)
    results = await asyncio.gather(
        *(psvc.payments.approve(ADMIN if n % 2 else OTHER_ADMIN, pid) for n in range(8)),
        return_exceptions=True,
    )
    kinds = sorted(getattr(r, "kind", repr(r)) for r in results)
    assert kinds.count("ok") == 1 and set(kinds) <= {"ok", "already_handled", "rate_limited"}, kinds
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    assert (
        await app_pool.fetchval("SELECT count(*) FROM admin_actions WHERE action='payment_approve'")
        == 1
    )


async def test_parallel_approve_and_reject_end_in_exactly_one_outcome(app_pool, psvc, shop):
    pid = await order_and_submit(psvc, 1, shop)
    approve, reject = await asyncio.gather(
        psvc.payments.approve(ADMIN, pid), psvc.payments.reject(OTHER_ADMIN, pid, "not received")
    )
    assert sorted([approve.kind, reject.kind]) == ["already_handled", "ok"]
    status = await app_pool.fetchval("SELECT status FROM payments")
    assert (status == "approved") == (
        await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    )


async def test_parallel_starts_never_create_two_open_orders(app_pool, psvc, shop, clock):
    results = await asyncio.gather(*(psvc.payments.start(1, shop) for _ in range(5)))
    assert {r.kind for r in results} <= {"ok", "busy", "rate_limited"}
    assert await app_pool.fetchval("SELECT count(*) FROM payments WHERE status='created'") == 1


# ------------------------------------------------------------------ J. expiry and housekeeping


async def test_premium_expires_by_itself_and_the_user_falls_back_to_free(app_pool, psvc, shop):
    pid = await order_and_submit(psvc, 1, shop)
    await psvc.payments.approve(ADMIN, pid)
    assert (await psvc.entitlements.get(1, "random_chat")).source == "subscription"
    await app_pool.execute(
        "UPDATE subscriptions SET current_period_end = now() - interval '1 second'"
    )
    assert (await psvc.entitlements.get(1, "random_chat")).source == "free"
    assert (await psvc.payments.overview(1)).premium_until is None
    assert (await psvc.payments.maintain())[2] == 1
    assert await app_pool.fetchval("SELECT status FROM subscriptions") == "expired"


async def test_maintenance_cancels_stale_orders_and_deletes_old_evidence_of_finished_payments(
    app_pool, psvc, shop
):
    stale = (await psvc.payments.start(1, shop)).order.payment_id
    await app_pool.execute("UPDATE payments SET created_at = now() - interval '25 hours'")
    done = await order_and_submit(psvc, 2, shop)
    await psvc.payments.reject(ADMIN, done, "not paid")
    await app_pool.execute(
        "ALTER TABLE payments DISABLE TRIGGER payments_guard_update"
    )  # time travel
    await app_pool.execute(
        "UPDATE payments SET reviewed_at = now() - interval '91 days' WHERE id=$1", done
    )
    await app_pool.execute("ALTER TABLE payments ENABLE TRIGGER payments_guard_update")
    waiting = await order_and_submit(psvc, 3, shop)
    await app_pool.execute(
        "UPDATE payments SET submitted_at = now() - interval '200 days' WHERE id=$1", waiting
    )
    cancelled, purged, _ = await psvc.payments.maintain()
    assert (cancelled, purged) == (1, 1)
    assert await app_pool.fetchval("SELECT status FROM payments WHERE id=$1", stale) == "cancelled"
    assert (
        await app_pool.fetchval("SELECT count(*) FROM payment_evidence WHERE payment_id=$1", done)
        == 0
    )
    assert (
        await app_pool.fetchval(
            "SELECT count(*) FROM payment_evidence WHERE payment_id=$1", waiting
        )
        == 1
    )


# ------------------------------------------------------------------ K. admin views and privacy


async def test_review_detail_shows_payment_facts_and_no_profile_or_chat_data(app_pool, psvc, shop):
    pid = await order_and_submit(psvc, 1, shop, utr="UTR123456789")
    other = await order_and_submit(psvc, 2, shop)
    detail = await psvc.payments.detail(ADMIN, pid)
    pay = detail["payment"]
    assert (pay["user_id"], pay["amount_minor"], pay["utr"], pay["plan_name"]) == (
        1,
        9900,
        "UTR123456789",
        "Premium 30 Days",
    )
    assert set(detail) == {"payment", "user_status", "has_evidence", "same_images", "same_utr"}
    assert set(pay.keys()).isdisjoint({"name", "bio", "city", "username", "age"})
    assert [r["id"] for r in detail["same_images"]] == [other]  # same screenshot bytes reused
    assert await psvc.payments.detail(5, pid) is None
    assert await psvc.payments.evidence(5, pid) is None
    ctype, data = await psvc.payments.evidence(ADMIN, pid)
    assert (ctype, data) == ("image/jpeg", img.JPEG)


async def test_listing_orders_pending_first_filters_and_paginates(app_pool, psvc, shop):
    a = await order_and_submit(psvc, 1, shop)
    b = await order_and_submit(psvc, 2, shop)
    await psvc.payments.approve(ADMIN, a)
    await psvc.payments.start(3, shop)  # an unsubmitted order is not a payment to review
    page = await psvc.payments.list_payments(ADMIN, status=None, page=0)
    assert [r["id"] for r in page.rows] == [b, a] and page.total == 2
    assert [
        r["id"] for r in (await psvc.payments.list_payments(ADMIN, status="approved", page=0)).rows
    ] == [a]
    assert (await psvc.payments.list_payments(5, status=None, page=0)).rows == []
    assert (await psvc.payments.list_payments(ADMIN, status="bogus", page=99)).total == 2
    counts = await psvc.payments.counts(ADMIN)
    assert (counts["pending"], counts["approved"], counts["rejected"]) == (1, 1, 0)


# ------------------------------------------------------------------ helpers


def test_amount_and_upi_link_helpers():
    assert [format_inr(x) for x in (9900, 9950, 100, 5)] == ["99", "99.50", "1", "0.05"]
    assert [
        parse_rupees(x)
        for x in ("99", "99.5", " 0.01 ", "1e3", "0", "-1", "9.999", "", "abc", "٩٩")
    ] == [9900, 9950, 1, None, None, None, None, None, None, None]
    uri = upi_uri("me@okhdfc", "A & B Shop", 9950, "PAY-ABCDEFGHIJ")
    assert uri == "upi://pay?pa=me@okhdfc&pn=A%20%26%20B%20Shop&am=99.50&cu=INR&tn=PAY-ABCDEFGHIJ"
