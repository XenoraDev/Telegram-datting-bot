"""Web admin: the Support pages (list, thread, reply, close) and what the user is told."""

import pytest

from tests.factories import make_user
from tests.web.conftest import ADMIN, OTHER_ADMIN


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw.get("reply_markup")))


@pytest.fixture
async def desk(admin):
    for uid in (1, 2):
        await make_user(admin.pool, uid)
    admin.state.bot = FakeBot()
    return admin


async def ticket(admin, uid=1, text="my payment is missing", category="payment"):
    res = await admin.state.services.tickets.open(uid, category, text)
    assert res.kind == "ok"
    return res.ticket_id


async def status(admin, tid):
    return await admin.pool.fetchval("SELECT status FROM support_tickets WHERE id = $1", tid)


# ------------------------------------------------------------------ pages


async def test_the_support_page_is_in_the_navigation_and_lists_by_status(desk):
    a = await ticket(desk, uid=1)
    b = await ticket(desk, uid=2, category="account")
    await desk.state.services.tickets.reply(ADMIN, b, "hello")
    home = (await desk.client.get("/admin/")).text
    assert 'href="/admin/support"' in home and ">Support<" in home
    default = (await desk.client.get("/admin/support")).text  # waiting for support first
    assert f"/admin/support/{a}" in default and f"/admin/support/{b}" not in default
    answered = (await desk.client.get("/admin/support?status=answered")).text
    assert f"/admin/support/{b}" in answered and f"/admin/support/{a}" not in answered
    everything = (await desk.client.get("/admin/support?status=all")).text
    assert "2 total" in everything and "payment" in everything and "account" in everything
    for junk in ("?status=<script>", "?status=%27%3B--", "?page=-5", "?page=abc"):
        assert (await desk.client.get(f"/admin/support{junk}")).status_code == 200


async def test_an_empty_list_says_so(desk):
    html = (await desk.client.get("/admin/support")).text
    assert "No tickets here" in html and "Nobody is waiting" in html


async def test_the_thread_shows_both_sides_and_escapes_what_the_user_wrote(desk):
    tid = await ticket(desk, text="<script>alert(1)</script> <img src=x onerror=alert(2)> & more")
    await desk.state.services.tickets.reply(ADMIN, tid, "we are looking")
    html = (await desk.client.get(f"/admin/support/{tid}")).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert" not in html
    assert "&lt;img src=x onerror=alert(2)&gt;" in html and "<img src=x" not in html
    assert "we are looking" in html and f"admin {ADMIN}" in html
    assert "Send reply" in html and "Close ticket" in html and 'name="csrf_token"' in html
    assert 'href="/admin/users/1"' in html


@pytest.mark.parametrize("path", ["/admin/support/99999", "/admin/support/0", "/admin/support/abc"])
async def test_unknown_tickets_are_404(desk, path):
    assert (await desk.client.get(path)).status_code == 404


async def test_a_closed_ticket_offers_no_reply_form(desk):
    tid = await ticket(desk)
    await desk.state.services.tickets.close(ADMIN, tid)
    html = (await desk.client.get(f"/admin/support/{tid}")).text
    assert "Send reply" not in html and "is closed" in html and " by admin" in html


# ------------------------------------------------------------------ replying


async def test_replying_stores_it_audits_it_and_tells_the_user(desk):
    tid = await ticket(desk)
    r = await desk.post(f"/admin/support/{tid}/reply", {"reply": "Please send your UTR number."})
    assert r.status_code == 303 and r.headers["location"] == f"/admin/support/{tid}?msg=tk_replied"
    assert "Reply sent" in (await desk.client.get(r.headers["location"])).text
    assert await status(desk, tid) == "answered"
    ((chat_id, text, _),) = desk.state.bot.sent
    assert chat_id == 1 and f"#{tid}" in text and "Please send your UTR number." in text
    (row,) = await desk.audit_rows()
    assert (row["admin_id"], row["action"], row["target_user_id"]) == (ADMIN, "ticket_reply", 1)
    assert row["detail"] == f"ticket #{tid}" and "UTR" not in str(dict(row))


async def test_the_user_is_told_in_their_own_language_and_html_is_escaped(desk):
    await desk.pool.execute("UPDATE users SET language = 'hi' WHERE id = 1")
    tid = await ticket(desk)
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "<b>done</b> & thanks"})
    ((_, text, _),) = desk.state.bot.sent
    assert "सहायता टीम ने आपके टिकट" in text and f"#{tid}" in text
    assert "&lt;b&gt;done&lt;/b&gt; &amp; thanks" in text and "<b>done" not in text


async def test_a_hindi_reply_near_the_limit_fits_the_form(desk):
    tid = await ticket(desk)
    reply = "नमस्ते " * 110  # ~770 characters, ~9 bytes each once form-encoded
    r = await desk.post(f"/admin/support/{tid}/reply", {"reply": reply.strip()})
    assert r.headers["location"].endswith("tk_replied"), r.status_code


@pytest.mark.parametrize(
    ("reply", "flash"), [("", "tk_empty"), ("   ", "tk_empty"), ("x" * 801, "tk_too_long")]
)
async def test_a_bad_reply_changes_nothing(desk, reply, flash):
    tid = await ticket(desk)
    r = await desk.post(f"/admin/support/{tid}/reply", {"reply": reply})
    assert r.headers["location"].endswith(f"msg={flash}")
    assert await status(desk, tid) == "open" and desk.state.bot.sent == []
    assert await desk.audit_rows() == []


async def test_replying_to_a_closed_or_missing_ticket_is_refused(desk):
    tid = await ticket(desk)
    await desk.post(f"/admin/support/{tid}/close")
    desk.state.bot.sent.clear()
    r = await desk.post(f"/admin/support/{tid}/reply", {"reply": "late"})
    assert r.headers["location"].endswith("msg=tk_closed") and desk.state.bot.sent == []
    r = await desk.post("/admin/support/99999/reply", {"reply": "x"})
    assert r.headers["location"].endswith("msg=missing")


async def test_another_admin_can_continue_the_thread(desk):
    tid = await ticket(desk)
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "first"})
    other = desk.client
    other.cookies.clear()
    await desk.login(OTHER_ADMIN)
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "second"})
    rows = await desk.pool.fetch(
        "SELECT admin_id FROM support_messages WHERE sender = 'admin' ORDER BY id"
    )
    assert [r["admin_id"] for r in rows] == [ADMIN, OTHER_ADMIN]


# ------------------------------------------------------------------ closing


async def test_closing_marks_it_audits_it_and_tells_the_user(desk):
    tid = await ticket(desk)
    r = await desk.post(f"/admin/support/{tid}/close")
    assert r.headers["location"] == f"/admin/support/{tid}?msg=tk_done"
    t = await desk.pool.fetchrow("SELECT status, closed_by FROM support_tickets")
    assert (t["status"], t["closed_by"]) == ("closed", "admin")
    ((chat_id, text, _),) = desk.state.bot.sent
    assert chat_id == 1 and "closed your ticket" in text and f"#{tid}" in text
    (row,) = await desk.audit_rows()
    assert (row["action"], row["detail"]) == ("ticket_close", f"ticket #{tid}")
    again = await desk.post(f"/admin/support/{tid}/close")
    assert again.headers["location"].endswith("msg=tk_closed")
    assert len(desk.state.bot.sent) == 1  # the user is told once


async def test_the_audit_page_shows_the_new_actions_without_any_ticket_text(desk):
    tid = await ticket(desk, text="very private words")
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "private answer"})
    await desk.post(f"/admin/support/{tid}/close")
    html = (await desk.client.get("/admin/audit")).text
    assert "ticket_reply" in html and "ticket_close" in html and f"ticket #{tid}" in html
    assert "very private words" not in html and "private answer" not in html


# ------------------------------------------------------------------ auth + CSRF


async def test_everything_needs_a_session(desk, web):
    tid = await ticket(desk)
    web.client.cookies.clear()
    for path in ("/admin/support", f"/admin/support/{tid}"):
        r = await web.client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login"
    for path in (f"/admin/support/{tid}/reply", f"/admin/support/{tid}/close"):
        r = await web.client.post(path, data={"reply": "x", "csrf_token": "whatever"})
        assert r.status_code == 403
    assert await status(desk, tid) == "open"


async def test_posts_need_the_csrf_token_and_our_own_origin(desk):
    tid = await ticket(desk)
    for path, data in (
        (f"/admin/support/{tid}/reply", {"reply": "x"}),
        (f"/admin/support/{tid}/close", {}),
    ):
        assert (await desk.post(path, data, with_csrf=False)).status_code == 403
        assert (
            await desk.post(path, {**data, "csrf_token": "bad"}, with_csrf=False)
        ).status_code == 403
        r = await desk.post(path, data, headers={"origin": "https://evil.example"})
        assert r.status_code == 403
    assert await status(desk, tid) == "open" and desk.state.bot.sent == []
    assert await desk.audit_rows() == []


# ------------------------------------------------------------------ the "Reply to support" button


def buttons(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


async def test_the_answer_carries_a_reply_button_bound_to_that_exact_ticket(desk):
    first = await ticket(desk, uid=1)
    second = await ticket(desk, uid=1, text="another problem")
    await desk.post(f"/admin/support/{first}/reply", {"reply": "we are on it"})
    ((chat, text, markup),) = desk.state.bot.sent
    assert chat == 1 and f"#{first}" in text and "Reply to support" in text
    assert buttons(markup) == [("💬 Reply to support", f"sp:l:{first}")]
    await desk.post(f"/admin/support/{second}/reply", {"reply": "and this one too"})
    assert buttons(desk.state.bot.sent[-1][2]) == [("💬 Reply to support", f"sp:l:{second}")]


async def test_the_button_text_is_in_the_users_language(desk):
    await desk.pool.execute("UPDATE users SET language = 'hi' WHERE id = 1")
    tid = await ticket(desk)
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "जाँच रहे हैं"})
    ((_, text, markup),) = desk.state.bot.sent
    assert buttons(markup) == [("💬 सहायता को जवाब दें", f"sp:l:{tid}")]
    assert f"#{tid}" in text


async def test_closing_ends_live_mode_and_the_closing_notice_has_no_reply_button(desk):
    tid = await ticket(desk)
    assert (await desk.state.services.tickets.start_live(1, tid)).kind == "ok"
    assert await desk.pool.fetchval("SELECT live_until FROM support_tickets") is not None
    await desk.post(f"/admin/support/{tid}/close", {})
    assert await desk.pool.fetchval("SELECT live_until FROM support_tickets") is None
    ((_, text, markup),) = desk.state.bot.sent
    assert "closed your ticket" in text
    assert not hasattr(markup, "inline_keyboard")  # the plain menu, no reply button
