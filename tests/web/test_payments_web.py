"""Web admin: payment review, payment settings and plans (P6)."""

import asyncio

import pytest

from tests import payment_images as img
from tests.factories import enable_payments, make_plan, make_user
from tests.web.conftest import ADMIN


class FakeBot:
    """Records what the web panel would send to users through Telegram."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@pytest.fixture
async def shop(admin):
    await enable_payments(admin.pool)
    plan = await make_plan(admin.pool)
    for uid in (1, 2, 3):
        await make_user(admin.pool, uid)
    admin.state.bot = FakeBot()
    return plan


async def submit(admin, uid, plan, *, utr=None, image=img.JPEG):
    svc = admin.state.services.payments
    pid = (await svc.start(uid, plan)).order.payment_id
    assert (await svc.attach_evidence(uid, pid, image)).kind == "ok"
    assert (await svc.submit(uid, pid, utr)).kind == "ok"
    return pid


async def status(admin, pid):
    return await admin.pool.fetchval("SELECT status FROM payments WHERE id=$1", pid)


# ------------------------------------------------------------------ review pages


async def test_pending_list_and_detail_show_everything_a_reviewer_needs(admin, shop):
    pid = await submit(admin, 1, shop, utr="utr123456789")
    ref = await admin.pool.fetchval("SELECT reference FROM payments")
    html = (await admin.client.get("/admin/payments")).text
    for needle in (
        ref,
        "pending review",
        "₹99",
        "UTR123456789",
        "Premium 30 Days",
        f"/admin/payments/{pid}",
    ):
        assert needle in html
    detail = (await admin.client.get(f"/admin/payments/{pid}")).text
    for needle in (
        "₹99",
        "INR",
        "UTR123456789",
        "shop@okbank",
        "30 days",
        f"/admin/payments/{pid}/evidence",
        "Approve payment",
        "Reject payment",
        "evidence only",
    ):
        assert needle in detail


async def test_tabs_filter_by_status_and_unsubmitted_orders_are_not_listed(admin, shop):
    a = await submit(admin, 1, shop)
    b = await submit(admin, 2, shop)
    await admin.post(f"/admin/payments/{a}/approve")
    await admin.state.services.payments.start(3, shop)  # opened but never submitted
    assert (await admin.client.get("/admin/payments")).text.count("/admin/payments/") >= 1
    pending = (await admin.client.get("/admin/payments?status=pending")).text
    assert f"/admin/payments/{b}" in pending and f"/admin/payments/{a}" not in pending
    approved = (await admin.client.get("/admin/payments?status=approved")).text
    assert f"/admin/payments/{a}" in approved and f"/admin/payments/{b}" not in approved
    everything = (await admin.client.get("/admin/payments?status=all")).text
    assert "2 total" in everything
    created = await admin.pool.fetchval("SELECT id FROM payments WHERE status='created'")
    assert (await admin.client.get(f"/admin/payments/{created}")).status_code == 404


async def test_the_screenshot_is_served_with_its_validated_type_and_safe_headers(admin, shop):
    pid = await submit(admin, 1, shop, image=img.PNG)
    r = await admin.client.get(f"/admin/payments/{pid}/evidence")
    assert r.status_code == 200 and r.content == img.PNG
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["content-disposition"].startswith("inline")


@pytest.mark.parametrize(
    "path",
    [
        "/admin/payments/99999",
        "/admin/payments/99999/evidence",
        "/admin/payments/0",
        "/admin/payments/abc",
        "/admin/payments/..%2f..%2fetc%2fpasswd/evidence",
    ],
)
async def test_unknown_payment_ids_are_404_and_never_touch_the_filesystem(admin, shop, path):
    assert (await admin.client.get(path)).status_code == 404


async def test_reviewer_sees_no_profile_chat_or_contact_data(admin, shop):
    await admin.pool.execute("UPDATE users SET username='private_handle' WHERE id=1")
    await admin.pool.execute(
        "UPDATE profiles SET bio='call 98765 43210 me@secret.example' WHERE user_id=1"
    )
    pid = await submit(admin, 1, shop)
    for path in (f"/admin/payments/{pid}", "/admin/payments"):
        html = (await admin.client.get(path)).text
        for secret in (
            "private_handle",
            "98765",
            "secret.example",
            "Name1",
            "Berlin",
            "TEST-TOKEN",
        ):
            assert secret not in html


async def test_duplicate_evidence_and_utr_are_flagged_for_the_reviewer(admin, shop):
    a = await submit(admin, 1, shop, utr="DUP000000001")
    b = await submit(admin, 2, shop)
    await admin.pool.execute("UPDATE payments SET utr='DUP000000001' WHERE id=$1", b)
    html = (await admin.client.get(f"/admin/payments/{b}")).text
    assert "Identical screenshot also on" in html and "Same UTR also on" in html
    assert f"/admin/payments/{a}" in html


async def test_user_supplied_and_admin_supplied_text_is_escaped(admin, shop):
    await admin.pool.execute(
        "UPDATE plans SET name = '<script>alert(1)</script>' WHERE id=$1", shop
    )
    pid = await submit(admin, 1, shop)
    await admin.post(f"/admin/payments/{pid}/reject", {"reason": "<img src=x onerror=alert(2)>"})
    for path in (
        f"/admin/payments/{pid}",
        "/admin/payments?status=all",
        "/admin/plans",
        "/admin/audit",
    ):
        html = (await admin.client.get(path)).text
        assert "<script>alert" not in html and "<img src=x" not in html


# ------------------------------------------------------------------ approve / reject


async def test_approve_activates_premium_audits_and_tells_the_user(admin, shop):
    pid = await submit(admin, 1, shop)
    assert await admin.pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    r = await admin.post(f"/admin/payments/{pid}/approve")
    assert (
        r.status_code == 303 and r.headers["location"] == f"/admin/payments/{pid}?msg=pay_approved"
    )
    assert await status(admin, pid) == "approved"
    sub = await admin.pool.fetchrow("SELECT * FROM subscriptions")
    assert (sub["user_id"], sub["plan_id"], sub["provider"]) == (1, shop, "manual_upi")
    pay = await admin.pool.fetchrow(
        "SELECT reviewed_by, reviewed_at, subscription_id FROM payments"
    )
    assert (
        pay["reviewed_by"] == ADMIN and pay["reviewed_at"] and pay["subscription_id"] == sub["id"]
    )
    audit = await admin.audit_rows()
    assert [(r["action"], r["admin_id"], r["target_user_id"]) for r in audit] == [
        ("payment_approve", ADMIN, 1)
    ]
    ((uid, text),) = admin.state.bot.sent
    assert uid == 1 and "approved" in text and "<b>" in text
    page = (await admin.client.get(f"/admin/payments/{pid}?msg=pay_approved")).text
    assert "Payment approved" in page and "Approve payment" not in page


async def test_approval_notice_is_in_the_users_language(admin, shop):
    await admin.pool.execute("UPDATE users SET language='hi' WHERE id=1")
    pid = await submit(admin, 1, shop)
    await admin.post(f"/admin/payments/{pid}/approve")
    assert "मंज़ूर हो गया" in admin.state.bot.sent[0][1]


async def test_approving_twice_does_not_create_a_second_subscription(admin, shop):
    pid = await submit(admin, 1, shop)
    await admin.post(f"/admin/payments/{pid}/approve")
    r = await admin.post(f"/admin/payments/{pid}/approve")
    assert r.headers["location"].endswith("?msg=pay_handled")
    assert await admin.pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    assert len(await admin.audit_rows()) == 1 and len(admin.state.bot.sent) == 1


async def test_simultaneous_approve_clicks_create_exactly_one_subscription(admin, shop):
    pid = await submit(admin, 1, shop)
    token = admin.token()
    results = await asyncio.gather(
        *(
            admin.client.post(f"/admin/payments/{pid}/approve", data={"csrf_token": token})
            for _ in range(6)
        )
    )
    assert all(r.status_code == 303 for r in results)
    assert await admin.pool.fetchval("SELECT count(*) FROM subscriptions") == 1
    assert (
        await admin.pool.fetchval(
            "SELECT count(*) FROM admin_actions WHERE action='payment_approve'"
        )
        == 1
    )
    assert len(admin.state.bot.sent) == 1


async def test_reject_needs_a_reason_and_never_activates_premium(admin, shop):
    pid = await submit(admin, 1, shop)
    for reason in ("", "  ", "ab"):
        r = await admin.post(f"/admin/payments/{pid}/reject", {"reason": reason})
        assert r.headers["location"].endswith("?msg=pay_reason")
    assert await status(admin, pid) == "pending" and admin.state.bot.sent == []
    r = await admin.post(
        f"/admin/payments/{pid}/reject", {"reason": "Amount received is Rs 50 only"}
    )
    assert r.headers["location"].endswith("?msg=pay_rejected")
    pay = await admin.pool.fetchrow("SELECT * FROM payments")
    assert (pay["status"], pay["reviewed_by"], pay["rejection_reason"]) == (
        "rejected",
        ADMIN,
        "Amount received is Rs 50 only",
    )
    assert await admin.pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    assert [r["action"] for r in await admin.audit_rows()] == ["payment_reject"]
    ((uid, text),) = admin.state.bot.sent
    assert uid == 1 and "Amount received is Rs 50 only" in text
    detail = (await admin.client.get(f"/admin/payments/{pid}")).text
    assert "Amount received is Rs 50 only" in detail and "Approve payment" not in detail


async def test_a_rejected_payment_cannot_be_approved_afterwards(admin, shop):
    pid = await submit(admin, 1, shop)
    await admin.post(f"/admin/payments/{pid}/reject", {"reason": "not received"})
    r = await admin.post(f"/admin/payments/{pid}/approve")
    assert r.headers["location"].endswith("?msg=pay_handled")
    assert await admin.pool.fetchval("SELECT count(*) FROM subscriptions") == 0


async def test_approval_of_a_banned_account_is_refused_with_a_clear_message(admin, shop):
    pid = await submit(admin, 1, shop)
    await admin.pool.execute("UPDATE users SET status='banned' WHERE id=1")
    r = await admin.post(f"/admin/payments/{pid}/approve")
    assert r.headers["location"].endswith("?msg=pay_user_inactive")
    assert await status(admin, pid) == "pending"


# ------------------------------------------------------------------ CSRF and authorisation


@pytest.mark.parametrize("action", ["approve", "reject"])
async def test_payment_decisions_need_csrf_and_a_session(admin, shop, action):
    pid = await submit(admin, 1, shop)
    body = {"reason": "nope nope"}
    r = await admin.client.post(f"/admin/payments/{pid}/{action}", data=body)  # no token
    assert r.status_code == 403
    r = await admin.client.post(
        f"/admin/payments/{pid}/{action}", data=body | {"csrf_token": "0" * 64}
    )
    assert r.status_code == 403
    r = await admin.post(
        f"/admin/payments/{pid}/{action}", body, headers={"Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    assert await status(admin, pid) == "pending" and await admin.audit_rows() == []
    admin.client.cookies.clear()
    r = await admin.client.post(
        f"/admin/payments/{pid}/{action}",
        data=body | {"csrf_token": admin.token() if admin.sid else "x"},
    )
    assert r.status_code == 403
    assert await status(admin, pid) == "pending"


async def test_get_requests_never_decide_a_payment(admin, shop):
    pid = await submit(admin, 1, shop)
    for verb in ("approve", "reject"):
        assert (await admin.client.get(f"/admin/payments/{pid}/{verb}")).status_code == 405
    assert await status(admin, pid) == "pending"


async def test_a_removed_admin_loses_access_to_every_payment_page(admin, shop):
    pid = await submit(admin, 1, shop)
    admin.state.sessions._admin_ids = frozenset({1})
    for path in (
        "/admin/payments",
        f"/admin/payments/{pid}",
        f"/admin/payments/{pid}/evidence",
        "/admin/plans",
        "/admin/payment-settings",
    ):
        assert (await admin.client.get(path)).status_code == 303
    r = await admin.client.post(
        f"/admin/payments/{pid}/approve", data={"csrf_token": admin.token()}
    )
    assert r.status_code == 403 and await status(admin, pid) == "pending"


# ------------------------------------------------------------------ payment settings


async def test_settings_page_shows_and_saves_the_configuration(admin):
    page = (await admin.client.get("/admin/payment-settings")).text
    assert 'name="upi_id"' in page and 'name="payee_name"' in page and "Payments active" in page
    r = await admin.post(
        "/admin/payment-settings",
        {
            "upi_id": "pay@okbank",
            "payee_name": "Xenora Meet",
            "instructions": "Pay exact amount.",
            "active": "1",
        },
    )
    assert r.headers["location"].endswith("?msg=settings_saved")
    row = await admin.pool.fetchrow("SELECT * FROM payment_settings")
    assert (row["upi_id"], row["payee_name"], row["is_active"], row["updated_by"]) == (
        "pay@okbank",
        "Xenora Meet",
        True,
        ADMIN,
    )
    assert 'value="pay@okbank"' in (await admin.client.get("/admin/payment-settings")).text
    assert [r["action"] for r in await admin.audit_rows()] == ["payment_config"]
    assert "pay@okbank" not in (await admin.audit_rows())[0]["detail"]


@pytest.mark.parametrize(
    "form,flash",
    [
        ({"upi_id": "nope", "payee_name": "X", "active": "1"}, "bad_upi_id"),
        ({"upi_id": "", "payee_name": "X", "active": "1"}, "bad_upi_id"),
        ({"upi_id": "ab@okbank", "payee_name": " ", "active": "1"}, "bad_payee_name"),
    ],
)
async def test_invalid_settings_are_refused_and_change_nothing(admin, form, flash):
    r = await admin.post("/admin/payment-settings", form)
    assert r.headers["location"].endswith(f"?msg={flash}")
    assert await admin.pool.fetchval("SELECT upi_id FROM payment_settings") is None
    assert await admin.audit_rows() == []


async def test_settings_need_csrf(admin):
    r = await admin.client.post(
        "/admin/payment-settings", data={"upi_id": "a@okbank", "payee_name": "X", "active": "1"}
    )
    assert r.status_code == 403
    assert await admin.pool.fetchval("SELECT upi_id FROM payment_settings") is None


async def test_switching_payments_off_stops_new_orders(admin, shop):
    await admin.post(
        "/admin/payment-settings", {"upi_id": "pay@okbank", "payee_name": "X", "active": ""}
    )
    assert (await admin.state.services.payments.start(1, shop)).kind == "unavailable"


# ------------------------------------------------------------------ plans


async def test_plans_can_be_created_edited_and_switched_off(admin):
    assert "No paid plans yet" in (await admin.client.get("/admin/plans")).text
    r = await admin.post(
        "/admin/plans",
        {"code": "gold_30", "name": "Gold", "days": "30", "price": "149.50", "active": "1"},
    )
    assert r.headers["location"].endswith("?msg=plan_saved")
    page = (await admin.client.get("/admin/plans")).text
    assert "gold_30" in page and 'value="149.50"' in page and "Gold" in page
    pid = await admin.pool.fetchval("SELECT id FROM plans WHERE code='gold_30'")
    r = await admin.post(
        f"/admin/plans/{pid}", {"name": "Gold+", "days": "60", "price": "199", "active": ""}
    )
    assert r.headers["location"].endswith("?msg=plan_saved")
    row = await admin.pool.fetchrow("SELECT * FROM plans WHERE id=$1", pid)
    assert (row["name"], row["duration_days"], row["price_minor"], row["is_active"]) == (
        "Gold+",
        60,
        19900,
        False,
    )
    assert [r["action"] for r in await admin.audit_rows()] == ["plan_save", "plan_save"]


@pytest.mark.parametrize(
    "form,flash",
    [
        ({"code": "BAD", "name": "x", "days": "30", "price": "99"}, "bad_code"),
        ({"code": "free", "name": "x", "days": "30", "price": "99"}, "bad_code"),
        ({"code": "ok", "name": "", "days": "30", "price": "99"}, "bad_name"),
        ({"code": "ok", "name": "x", "days": "0", "price": "99"}, "bad_days"),
        ({"code": "ok", "name": "x", "days": "30", "price": "0"}, "bad_price"),
        ({"code": "ok", "name": "x", "days": "30", "price": "1e3"}, "bad_price"),
    ],
)
async def test_invalid_plans_are_refused(admin, form, flash):
    r = await admin.post("/admin/plans", form)
    assert r.headers["location"].endswith(f"?msg={flash}")
    assert await admin.pool.fetchval("SELECT count(*) FROM plans") == 1


async def test_duplicate_code_and_free_plan_edits_are_refused(admin):
    form = {"code": "p1", "name": "P", "days": "30", "price": "99", "active": "1"}
    await admin.post("/admin/plans", form)
    assert (await admin.post("/admin/plans", form)).headers["location"].endswith("?msg=plan_exists")
    free = await admin.pool.fetchval("SELECT id FROM plans WHERE code='free'")
    r = await admin.post(f"/admin/plans/{free}", {"name": "x", "days": "1", "price": "1"})
    assert r.headers["location"].endswith("?msg=missing")
    assert await admin.pool.fetchval("SELECT price_minor FROM plans WHERE code='free'") is None


async def test_plan_changes_need_csrf(admin):
    r = await admin.client.post(
        "/admin/plans", data={"code": "p1", "name": "P", "days": "30", "price": "99"}
    )
    assert r.status_code == 403
    assert await admin.pool.fetchval("SELECT count(*) FROM plans") == 1


# ------------------------------------------------------------------ dashboard, audit, privacy


async def test_dashboard_counts_payments_awaiting_review(admin, shop):
    await submit(admin, 1, shop)
    b = await submit(admin, 2, shop)
    await admin.post(f"/admin/payments/{b}/reject", {"reason": "not received"})
    html = (await admin.client.get("/admin/")).text
    assert "<span>Awaiting review</span><strong>1</strong>" in html
    assert "<span>Rejected</span><strong>1</strong>" in html


async def test_new_actions_appear_in_the_audit_log_and_can_be_filtered(admin, shop):
    a = await submit(admin, 1, shop)
    await admin.post(f"/admin/payments/{a}/approve")
    await admin.post(
        "/admin/payment-settings", {"upi_id": "pay@okbank", "payee_name": "X", "active": "1"}
    )
    html = (await admin.client.get("/admin/audit")).text
    for action in ("payment_approve", "payment_config"):
        assert f"<td>{action}</td>" in html
    only = (await admin.client.get("/admin/audit?action=payment_approve")).text
    assert "1 total" in only
    for method in ("post", "put", "delete"):
        assert (await getattr(admin.client, method)("/admin/audit")).status_code in (403, 405)


async def test_there_is_no_route_for_payment_secrets_or_files_on_disk(admin):
    routes = set()
    for route in admin.app.routes:
        routes |= {getattr(route, "path", "")} | {
            getattr(s, "path", "") for s in getattr(route, "routes", [])
        }
    paths = " ".join(routes).lower()
    for word in ("upload", "download", "file", "media"):
        assert word not in paths
