"""Admin forms as a real browser submits them (regression: every POST answered 403 in production).

Root cause: pages were served with `Referrer-Policy: no-referrer`. For a request with that policy a
browser sends `Origin: null` (and no Referer) on same-origin form POSTs (Fetch spec; confirmed in
Chromium), and the CSRF Origin check rightly refuses `null`. The fix is the response header
(`same-origin`), NOT a weaker Origin check: `Origin: null` is still rejected below.
"""

import re

import pytest

from tests.factories import make_user
from tests.web.conftest import DOMAIN, Web
from web.app import security

SITE = f"https://{DOMAIN}"
FORM = {"upi_id": "shop@okbank", "payee_name": "Test Payee", "instructions": "Pay.", "active": "1"}


def browser_headers(page_headers, page_url: str) -> dict[str, str]:
    """The Origin / Referer a browser attaches to a same-origin form POST from that page."""
    policy = page_headers.get("referrer-policy", "")
    if policy == "no-referrer":  # Fetch spec: the request origin is serialised as opaque
        return {"Origin": "null"}
    return {"Origin": SITE, "Referer": SITE + page_url}


async def submit_form(web: Web, page: str, action: str, fields: dict[str, str]):
    """Open `page`, find the form posting to `action`, fill it in and submit like a browser."""
    shown = await web.client.get(page)
    assert shown.status_code == 200, shown.text
    form = re.search(rf'<form[^>]*action="{re.escape(action)}"[^>]*>(.*?)</form>', shown.text, re.S)
    assert form, f"no form posting to {action} on {page}"
    hidden = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', form.group(1)))
    return await web.client.post(
        action, data={**hidden, **fields}, headers=browser_headers(shown.headers, page)
    )


async def saved(web: Web):
    return await web.pool.fetchval("SELECT upi_id FROM payment_settings")


# ------------------------------------------------------------------ the fix


async def test_payment_settings_form_is_saved_when_submitted_like_a_browser(admin):
    r = await submit_form(admin, "/admin/payment-settings", "/admin/payment-settings", FORM)
    assert r.status_code == 303 and r.headers["location"].endswith("?msg=settings_saved")
    row = await admin.pool.fetchrow("SELECT * FROM payment_settings")
    assert (row["upi_id"], row["payee_name"], row["is_active"]) == (
        "shop@okbank",
        "Test Payee",
        True,
    )
    assert [r["action"] for r in await admin.audit_rows()] == ["payment_config"]


async def test_the_original_failure_a_no_referrer_page_makes_browsers_send_origin_null(
    admin, monkeypatch
):
    """Reproduces production: with the old header the same form POST is answered 403."""
    old = [(k, b"no-referrer" if k == b"referrer-policy" else v) for k, v in security.HEADERS]
    monkeypatch.setattr(security, "HEADERS", old)
    r = await submit_form(admin, "/admin/payment-settings", "/admin/payment-settings", FORM)
    assert r.status_code == 403
    assert await saved(admin) is None and await admin.audit_rows() == []


@pytest.mark.parametrize(
    "page,action,fields",
    [
        ("/admin/plans", "/admin/plans", {"code": "p1", "name": "P", "days": "30", "price": "99"}),
        ("/admin/users/1", "/admin/users/1/unban", {}),
        ("/admin/users/1/ban", "/admin/users/1/ban", {"note": "spam"}),
        ("/admin/", "/logout", {}),
    ],
)
async def test_every_kind_of_admin_form_works_for_a_browser(admin, page, action, fields):
    await make_user(admin.pool, 1)
    if action.endswith("/unban"):
        await admin.pool.execute("UPDATE users SET status='banned' WHERE id=1")
    r = await submit_form(admin, page, action, fields)
    assert r.status_code == 303, (action, r.status_code)


async def test_report_and_payment_decision_forms_work_for_a_browser(admin):
    from tests import payment_images as img
    from tests.factories import enable_payments, make_plan
    from tests.web.conftest import file_report

    await enable_payments(admin.pool)
    plan = await make_plan(admin.pool)
    for uid in (1, 2):
        await make_user(admin.pool, uid)
    rid = await file_report(admin.pool, 2, 1)
    r = await submit_form(admin, f"/admin/reports/{rid}", f"/admin/reports/{rid}/dismiss", {})
    assert r.status_code == 303 and r.headers["location"].endswith("msg=dismissed")
    svc = admin.state.services.payments
    pid = (await svc.start(1, plan)).order.payment_id
    await svc.attach_evidence(1, pid, img.JPEG)
    await svc.submit(1, pid, None)
    page = f"/admin/payments/{pid}"
    r = await submit_form(admin, page, f"{page}/reject", {"reason": "not received"})
    assert r.headers["location"].endswith("msg=pay_rejected")


@pytest.mark.parametrize(
    "path", ["/login", "/admin/", "/admin/payment-settings", "/admin/plans", "/nope"]
)
async def test_no_page_asks_browsers_to_hide_their_origin(admin, path):
    policy = (await admin.client.get(path)).headers["referrer-policy"]
    assert policy == "same-origin"  # never "no-referrer": that turns Origin into "null"


# ------------------------------------------------------------------ security is not weakened


@pytest.mark.parametrize(
    "headers,ok",
    [
        ({"Origin": SITE}, True),
        ({"Origin": SITE, "Referer": SITE + "/admin/payment-settings"}, True),
        ({"Referer": SITE + "/admin/payment-settings"}, True),
        ({}, True),  # non-browser client: no Origin at all; the CSRF token still decides
        ({"Origin": "null"}, False),
        ({"Origin": "https://evil.example"}, False),
        ({"Origin": f"http://{DOMAIN}"}, False),  # downgrade
        ({"Origin": f"https://{DOMAIN}.evil.example"}, False),
        ({"Origin": f"https://evil.example/{DOMAIN}"}, False),
        ({"Origin": SITE, "Referer": "https://evil.example/x"}, False),
        ({"Referer": f"http://{DOMAIN}/admin/payment-settings"}, False),
    ],
)
async def test_origin_and_referer_matrix_with_a_valid_session_and_token(admin, headers, ok):
    r = await admin.post("/admin/payment-settings", FORM, headers=headers)
    assert (r.status_code == 303) is ok, (headers, r.status_code)
    assert (await saved(admin) == "shop@okbank") is ok


@pytest.mark.parametrize("token", [None, "", "0" * 64, "A" * 300])
async def test_a_good_origin_does_not_replace_the_csrf_token(admin, token):
    data = dict(FORM) | ({} if token is None else {"csrf_token": token})
    r = await admin.client.post("/admin/payment-settings", data=data, headers={"Origin": SITE})
    assert r.status_code == 403 and await saved(admin) is None


async def test_a_good_origin_and_token_do_not_replace_the_session(web):
    await web.login()
    token = web.token()
    web.client.cookies.clear()
    r = await web.client.post(
        "/admin/payment-settings", data=FORM | {"csrf_token": token}, headers={"Origin": SITE}
    )
    assert r.status_code == 403 and await saved(web) is None


async def test_settings_are_saved_only_by_a_fully_valid_request(admin):
    token = admin.token()
    bad = [
        admin.client.post(
            "/admin/payment-settings", data=FORM, headers={"Origin": SITE}
        ),  # no token
        admin.client.post(
            "/admin/payment-settings", data=FORM | {"csrf_token": "x"}, headers={"Origin": SITE}
        ),
        admin.client.post(
            "/admin/payment-settings", data=FORM | {"csrf_token": token}, headers={"Origin": "null"}
        ),
    ]
    for pending in bad:
        assert (await pending).status_code == 403
        assert await saved(admin) is None
    good = await admin.client.post(
        "/admin/payment-settings", data=FORM | {"csrf_token": token}, headers={"Origin": SITE}
    )
    assert good.status_code == 303 and await saved(admin) == "shop@okbank"
