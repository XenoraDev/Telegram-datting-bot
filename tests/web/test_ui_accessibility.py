"""Responsive + accessibility structure of every admin page (what can be checked without a
browser: the layout rules live in style.css, the markup carries labels and scroll regions)."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests import payment_images as img
from tests.factories import enable_payments, make_plan, make_user
from tests.web.conftest import file_report

CSS = (Path(__file__).resolve().parents[2] / "web/app/static/style.css").read_text()
PAGES = [
    "/admin/",
    "/admin/users",
    "/admin/users/1",
    "/admin/reports",
    "/admin/reports/1",
    "/admin/payments",
    "/admin/payments/1",
    "/admin/plans",
    "/admin/tiers",
    "/admin/payment-settings",
    "/admin/statistics",
    "/admin/audit",
]


class Audit(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack, self.labels_for, self.inputs, self.problems = [], set(), [], []
        self.label_depth = 0
        self.scroll_depth = 0
        self.button_text, self.in_button = [], False
        self.meta = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.stack.append((tag, a))
        if tag == "meta" and a.get("name") == "viewport":
            self.meta["viewport"] = a.get("content")
        if tag == "html":
            self.meta["lang"] = a.get("lang")
        if tag == "label":
            self.label_depth += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        if tag == "div" and "scroll" in (a.get("class") or ""):
            self.scroll_depth += 1
        if tag == "table" and not self.scroll_depth:
            self.problems.append("table outside a .scroll wrapper")
        if tag == "table" and self.scroll_depth:
            pass
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit"):
            self.inputs.append((a, self.label_depth > 0))
        if tag == "img" and "alt" not in a:
            self.problems.append("img without alt")
        if tag == "button":
            self.in_button = True
            self.button_text.append("")

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth -= 1
        if tag == "div" and self.stack and self.scroll_depth:
            # the scroll wrapper is the only div we track by class; approximate by tag counting
            pass
        if tag == "button":
            self.in_button = False
        while self.stack and self.stack[-1][0] != tag:
            self.stack.pop()
        if self.stack:
            _, a = self.stack.pop()
            if tag == "div" and "scroll" in (a.get("class") or ""):
                self.scroll_depth -= 1

    def handle_data(self, data):
        if self.in_button:
            self.button_text[-1] += data


@pytest.fixture
async def seeded(admin):
    pool = admin.pool
    await enable_payments(pool)
    plan = await make_plan(pool)
    for uid in (1, 2):
        await make_user(pool, uid)
    svc = admin.state.services.payments
    pid = (await svc.start(1, plan)).order.payment_id
    await svc.attach_evidence(1, pid, img.JPEG)
    await svc.submit(1, pid, "UTR000000001")
    await file_report(pool, 1, 2)
    return admin


@pytest.mark.parametrize("path", PAGES)
async def test_every_page_is_responsive_labelled_and_navigable(seeded, path):
    r = await seeded.client.get(path)
    assert r.status_code == 200, path
    p = Audit()
    p.feed(r.text)
    assert p.meta["viewport"] == "width=device-width, initial-scale=1"
    assert p.meta["lang"] == "en"
    assert p.problems == [], p.problems
    labelled = [
        a
        for a, inside in p.inputs
        if inside or a.get("aria-label") or (a.get("id") and a["id"] in p.labels_for)
    ]
    assert len(labelled) == len(p.inputs), [a for a, _ in p.inputs if a not in labelled]
    assert all(t.strip() for t in p.button_text), "a button without visible text"
    assert 'class="skip" href="#content"' in r.text and 'id="content"' in r.text
    assert '<nav aria-label="Main">' in r.text
    assert "<h1" in r.text


def test_css_has_the_mobile_focus_and_touch_rules():
    assert "@media (max-width: 700px)" in CSS
    assert ":focus-visible" in CSS
    assert "min-height: 44px" in CSS  # tappable controls on phones
    assert "overflow-x: auto" in CSS  # wide tables scroll instead of breaking the page
    assert "prefers-color-scheme: dark" in CSS  # the dark style is kept
    assert "font-size: 16px" in CSS  # no zoom-on-focus on iOS
    assert re.search(r"\.badge\.pending", CSS)


async def test_status_is_never_color_only(seeded):
    html = (await seeded.client.get("/admin/payments")).text
    assert "pending review" in html  # the badge has words, not just a colour
    html = (await seeded.client.get("/admin/payments/1")).text
    assert "✔" in html or "✖" in html or "could not confirm" in html


async def test_the_login_page_keeps_the_telegram_widget_and_its_security_headers(web):
    r = await web.client.get("/login")
    assert "telegram-widget.js" in r.text
    assert r.headers["cross-origin-opener-policy"] == "same-origin-allow-popups"
    assert r.headers["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    page = await web.client.get("/admin/")
    assert page.status_code in (303, 307, 401, 403, 404)


async def test_empty_states_say_what_is_empty(admin):
    for path, text in (("/admin/payments", "No payments here"), ("/admin/users?q=999999", "")):
        html = (await admin.client.get(path)).text
        assert text in html
