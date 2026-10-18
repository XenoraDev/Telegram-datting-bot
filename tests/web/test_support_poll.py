"""Support inbox live updates: the JSON polling API, its security, and the no-duplicate button."""

from pathlib import Path

import pytest

from tests.factories import make_user
from tests.web.conftest import ADMIN

JS = Path("web/app/static/support.js")


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


async def ticket(admin, uid=1, text="my payment is missing"):
    res = await admin.state.services.tickets.open(uid, "payment", text)
    assert res.kind == "ok"
    return res.ticket_id


def ids(r):
    return [m["id"] for m in r.json()["messages"]]


# ------------------------------------------------------------------ polling API


async def test_polling_returns_only_messages_after_the_cursor_and_never_repeats(desk):
    tid = await ticket(desk)
    first = await desk.client.get(f"/admin/support/api/{tid}/messages?after=0")
    assert first.status_code == 200 and len(ids(first)) == 1
    cursor = ids(first)[-1]
    again = await desk.client.get(f"/admin/support/api/{tid}/messages?after={cursor}")
    assert again.json()["messages"] == []  # nothing new, nothing repeated
    await desk.state.services.tickets.reply(ADMIN, tid, "we are checking")
    nxt = await desk.client.get(f"/admin/support/api/{tid}/messages?after={cursor}")
    assert [m["text"] for m in nxt.json()["messages"]] == ["we are checking"]
    assert nxt.json()["messages"][0]["sender"] == "admin"
    again = await desk.client.get(f"/admin/support/api/{tid}/messages?after={ids(nxt)[-1]}")
    assert again.json()["messages"] == []


@pytest.mark.parametrize("bad", ["", "abc", "-5", "1e9", "9" * 40, "%27;--", "<script>"])
async def test_a_bad_cursor_is_treated_as_zero_not_an_error(desk, bad):
    tid = await ticket(desk)
    r = await desk.client.get(f"/admin/support/api/{tid}/messages?after={bad}")
    assert r.status_code == 200 and len(ids(r)) == 1


async def test_polling_never_leaks_another_ticket(desk):
    a = await ticket(desk, uid=1, text="mine is about A")
    b = await ticket(desk, uid=2, text="secret about B")
    r = await desk.client.get(f"/admin/support/api/{a}/messages?after=0")
    assert "secret about B" not in r.text and "mine is about A" in r.text
    other = await desk.client.get(f"/admin/support/api/{b}/messages?after=0")
    assert "secret about B" in other.text and "mine is about A" not in other.text  # switching


async def test_polling_unknown_tickets_is_404_json(desk):
    r = await desk.client.get("/admin/support/api/999999/messages?after=0")
    assert r.status_code == 404 and r.json() == {"error": "not_found"}


async def test_the_api_needs_an_admin_session_and_answers_json_401(desk, web):
    tid = await ticket(desk)
    web.client.cookies.clear()
    for path in (f"/admin/support/api/{tid}/messages", "/admin/support/api/tickets"):
        r = await web.client.get(path)
        assert r.status_code == 401 and r.headers["content-type"].startswith("application/json")
        assert "my payment" not in r.text
    r = await web.client.post(f"/admin/support/api/{tid}/reply", data={"reply": "x"})
    assert r.status_code in (401, 403)


async def test_the_list_api_reflects_new_activity_and_filters_by_status(desk):
    a = await ticket(desk, uid=1)
    b = await ticket(desk, uid=2, text="other")
    await desk.state.services.tickets.reply(ADMIN, b, "hello")
    waiting = (await desk.client.get("/admin/support/api/tickets?status=open")).json()["tickets"]
    assert [t["id"] for t in waiting] == [a] and waiting[0]["last_sender"] == "user"
    answered = (await desk.client.get("/admin/support/api/tickets?status=answered")).json()
    assert [t["id"] for t in answered["tickets"]] == [b]
    assert answered["tickets"][0]["last_sender"] == "admin" and answered["tickets"][0]["last_id"]


# ------------------------------------------------------------------ replying through the API


async def test_the_api_reply_stores_it_tells_the_user_and_returns_the_message(desk):
    tid = await ticket(desk)
    r = await desk.post(f"/admin/support/api/{tid}/reply", {"reply": "<b>hi</b>"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["message"]["text"] == "<b>hi</b>"  # raw text travels as JSON, never as HTML
    assert len(desk.state.bot.sent) == 1 and desk.state.bot.sent[0][0] == 1
    poll = await desk.client.get(f"/admin/support/api/{tid}/messages?after=0")
    assert [m["sender"] for m in poll.json()["messages"]] == ["user", "admin"]


async def test_the_api_reply_needs_csrf_and_our_origin(desk):
    tid = await ticket(desk)
    path = f"/admin/support/api/{tid}/reply"
    assert (await desk.post(path, {"reply": "x"}, with_csrf=False)).status_code == 403
    r = await desk.post(path, {"reply": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert desk.state.bot.sent == []
    assert (
        len((await desk.client.get(f"/admin/support/api/{tid}/messages")).json()["messages"]) == 1
    )


async def test_the_api_cannot_reply_to_a_closed_or_missing_ticket(desk):
    tid = await ticket(desk)
    await desk.post(f"/admin/support/{tid}/close", {})
    r = await desk.post(f"/admin/support/api/{tid}/reply", {"reply": "late"})
    assert r.status_code == 422 and r.json()["ok"] is False
    r = await desk.post("/admin/support/api/999999/reply", {"reply": "x"})
    assert r.status_code == 404


async def test_closing_still_needs_csrf(desk):
    tid = await ticket(desk)
    assert (await desk.post(f"/admin/support/{tid}/close", {}, with_csrf=False)).status_code == 403


# ------------------------------------------------------------------ no duplicate reply button


def buttons(markup):
    return [
        b.callback_data for row in (getattr(markup, "inline_keyboard", None) or []) for b in row
    ]


async def test_no_second_reply_button_while_the_user_is_live(desk):
    tid = await ticket(desk)
    assert (await desk.state.services.tickets.start_live(1, tid)).kind == "ok"
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "first"})
    await desk.post(f"/admin/support/api/{tid}/reply", {"reply": "second"})
    assert len(desk.state.bot.sent) == 2
    for _, text, markup in desk.state.bot.sent:
        assert buttons(markup) == [] and f"#{tid}" in text
    assert await desk.state.services.tickets.is_live(1, tid)  # still live afterwards


async def test_the_button_returns_when_live_expired_stopped_or_for_another_ticket(desk):
    tid = await ticket(desk)
    other = await ticket(desk, text="second problem")
    await desk.state.services.tickets.start_live(1, tid)
    await desk.pool.execute(
        "UPDATE support_tickets SET live_until = now() - interval '1 minute' WHERE id = $1", tid
    )
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "expired"})
    await desk.state.services.tickets.start_live(1, tid)
    await desk.state.services.tickets.stop_live(1)
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "stopped"})
    await desk.state.services.tickets.start_live(1, other)
    await desk.post(f"/admin/support/{tid}/reply", {"reply": "elsewhere"})
    assert [buttons(m) for _, _, m in desk.state.bot.sent] == [[f"sp:l:{tid}"]] * 3


async def test_a_closed_ticket_cannot_go_live(desk):
    tid = await ticket(desk)
    await desk.post(f"/admin/support/{tid}/close", {})
    assert (await desk.state.services.tickets.start_live(1, tid)).kind != "ok"
    assert not await desk.state.services.tickets.is_live(1, tid)


async def test_the_live_wording_has_both_languages(desk):
    from bot.texts.i18n import tr

    en = tr("support.admin_replied_live", "en", id=7, reply="x")
    hi = tr("support.admin_replied_live", "hi", id=7, reply="x")
    assert "#7" in en and "#7" in hi and en != hi


# ------------------------------------------------------------------ pages, CSP, script


async def test_the_inbox_pages_have_the_two_pane_layout_and_a_composer(desk):
    tid = await ticket(desk)
    html = (await desk.client.get(f"/admin/support/{tid}")).text
    for needle in (
        'id="ticket-list"',
        'id="reply-form"',
        'id="reply"',
        'id="send-btn"',
        'id="live-state"',
        f'data-id="{tid}"',
        "/static/support.js",
        "Close ticket",
    ):
        assert needle in html, needle
    assert 'id="ticket-list"' in (await desk.client.get("/admin/support")).text


async def test_support_csp_allows_only_our_own_scripts(desk):
    tid = await ticket(desk)
    for path in ("/admin/support", f"/admin/support/{tid}", f"/admin/support/api/{tid}/messages"):
        csp = (await desk.client.get(path)).headers["content-security-policy"]
        assert "script-src 'self'" in csp
        assert "unsafe-inline" not in csp.split("style-src")[0] and "unsafe-eval" not in csp
        assert "'unsafe-inline'" not in csp and "default-src 'none'" in csp
    other = (await desk.client.get("/admin/")).headers["content-security-policy"]
    assert "script-src" not in other  # every other admin page stays script-free


def test_the_script_never_builds_html_from_server_text():
    src = JS.read_text()
    for banned in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ):
        assert banned not in src, banned
    assert "textContent" in src
