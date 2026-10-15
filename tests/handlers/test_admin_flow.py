"""P3 through the real Dispatcher: /admin dashboard, authorization of every entry point."""

import json

import pytest

from bot.texts import en as t
from tests.factories import make_user
from tests.handlers.harness import ADMIN_ID as ADMIN

U1, U2 = 7000001, 7000002


def buttons(out):
    return [
        b.callback_data
        for o in out
        for row in getattr(o.markup, "inline_keyboard", [])
        for b in row
    ]


def texts(out):
    return [o.text for o in out if o.kind in ("SendMessage", "EditMessageText")]


async def report(pool, reporter, reported, *, context=None, reason="spam"):
    return await pool.fetchval(
        "INSERT INTO reports (reporter_id, reported_id, source, reason, context) "
        "VALUES ($1, $2, 'profile', $3, $4::jsonb) RETURNING id",
        reporter,
        reported,
        reason,
        json.dumps(context) if context else None,
    )


@pytest.fixture
async def adm(tg):
    await tg.onboard(ADMIN)
    return tg


# ---------------- authorization ----------------


async def test_non_admin_gets_a_generic_denial_and_no_dashboard(tg):
    await tg.onboard(U1)
    out = await tg.send(U1, "/admin")
    assert texts(out) == [t.ADMIN_DENIED] and buttons(out) == []
    assert str(ADMIN) not in "".join(o.text for o in tg.out)  # ADMIN_IDS never leaked


async def test_unknown_account_cannot_reach_admin_either(tg):
    out = await tg.send(U1, "/admin")
    assert texts(out) == [t.NEED_START]


@pytest.mark.parametrize(
    "data", ["ad:home", "ad:st", "ad:find", "ad:rl:0", "ad:u:1", "ad:r:1", "ad:bo:2:0", "ad:ub:2"]
)
async def test_every_admin_button_is_refused_for_non_admins(tg, app_pool, data):
    await tg.onboard(U1)
    await tg.onboard(U2)
    await report(app_pool, U1, U2)
    out = await tg.press(U1, data)
    answers = [o.method for o in out if o.kind == "AnswerCallbackQuery"]
    assert answers and answers[0].text == t.ADMIN_DENIED and answers[0].show_alert
    assert texts(out) == []  # nothing else is sent or edited
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = $1", U2) == "active"
    assert await app_pool.fetchval("SELECT count(*) FROM admin_actions") == 0


async def test_forged_payload_cannot_escalate(tg, app_pool):
    await tg.onboard(U1)
    for data in (f"ad:u:{ADMIN}", f"ad:bo:{U1}:0", "ad:home:admin", f"ad:st:{ADMIN}"):
        await tg.press(U1, data)
    await tg.send(U1, f"/user {ADMIN}")
    assert await app_pool.fetchval("SELECT count(*) FROM admin_actions") == 0
    assert (await tg.send(U1, "/admin"))[0].text == t.ADMIN_DENIED


async def test_user_id_text_input_is_ignored_unless_an_admin_asked_for_it(tg):
    await tg.onboard(U1)
    out = await tg.send(U1, f"{U1}")
    assert t.ADMIN_DENIED not in texts(out)  # a normal message: not an admin prompt


async def test_admin_state_does_not_leak_to_a_demoted_user(tg):
    await tg.onboard(ADMIN)
    await tg.press(ADMIN, "ad:find")
    # same id is no longer an admin (settings changed): numeric input must not be honoured
    tg.dp.workflow_data["settings"] = type(tg.dp.workflow_data["settings"])(
        bot_token="1:x", database_url="postgresql://u:p@h/d"
    )
    out = await tg.send(ADMIN, str(ADMIN))
    assert texts(out) == [t.ADMIN_DENIED]


# ---------------- dashboard ----------------


async def test_admin_dashboard_has_users_reports_statistics_buttons(adm, app_pool):
    await adm.onboard(U1)
    await report(app_pool, U1, ADMIN)
    out = await adm.send(ADMIN, "/admin")
    assert texts(out) == [t.ADMIN_HOME]
    assert buttons(out) == ["ad:find", "ad:rl:0", "ad:st"]
    labels = [b.text for row in out[0].markup.inline_keyboard for b in row]
    assert labels == ["👥 Users", "🚨 Reports (1)", "📊 Statistics"]


async def test_statistics_screen_and_back_navigation(adm, app_pool):
    await adm.onboard(U1)
    out = await adm.press(ADMIN, "ad:st")
    text = texts(out)[0]
    assert "Statistics" in text and "Users: 2" in text and "active 2" in text
    assert buttons(out) == ["ad:home"]
    back = await adm.press(ADMIN, "ad:home")
    assert texts(back) == [t.ADMIN_HOME]


# ---------------- user lookup / ban / unban ----------------


async def test_find_user_by_id_shows_moderation_info(adm, app_pool):
    await adm.onboard(U1, name="Mia", city="Rome", bio="private bio text")
    out = await adm.press(ADMIN, "ad:find")
    assert texts(out) == [t.ADMIN_ASK_ID]
    out = await adm.send(ADMIN, str(U1))
    text = texts(out)[0]
    assert f"User {U1}" in text and "Status: <b>active</b>" in text
    assert "18+ confirmed: yes" in text and "Profile: yes" in text and "Mia" in text
    assert buttons(out) == [f"ad:b:{U1}:0", "ad:home"]


async def test_user_command_and_invalid_or_unknown_ids(adm):
    assert texts(await adm.send(ADMIN, "/user abc")) == [t.ADMIN_BAD_ID]
    assert texts(await adm.send(ADMIN, "/user")) == [t.ADMIN_BAD_ID]
    assert texts(await adm.send(ADMIN, "/user 1; DROP TABLE users")) == [t.ADMIN_BAD_ID]
    assert texts(await adm.send(ADMIN, "/user -5")) == [t.ADMIN_BAD_ID]
    assert texts(await adm.send(ADMIN, "/user 999999")) == [t.ADMIN_NO_USER]
    await adm.press(ADMIN, "ad:find")
    assert texts(await adm.send(ADMIN, "9" * 30)) == [t.ADMIN_BAD_ID]  # too long, state kept
    assert texts(await adm.send(ADMIN, "0")) == [t.ADMIN_BAD_ID]


async def test_ban_needs_confirmation_then_blocks_the_user_and_ends_the_chat(adm, app_pool):
    await adm.onboard(U1)
    await adm.onboard(U2)
    await adm.send(U1, "/chat")
    await adm.send(U2, "/chat")
    ask = await adm.press(ADMIN, f"ad:b:{U1}:0")
    assert buttons(ask) == [f"ad:bo:{U1}:0", f"ad:u:{U1}"]
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = $1", U1) == "active"
    out = await adm.press(ADMIN, f"ad:bo:{U1}:0")
    assert t.ADMIN_BANNED in texts(out) and "banned" in " ".join(texts(out))
    assert t.CHAT_ENDED_PARTNER in [o.text for o in out if o.chat_id == U2]
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = $1", U1) == "banned"
    assert (await adm.send(U1, "/chat"))[0].text == t.BLOCKED_BANNED
    assert (await adm.send(U1, "hello"))[0].text == t.BLOCKED_BANNED


async def test_unban_restores_access(adm, app_pool):
    await adm.onboard(U1)
    await adm.press(ADMIN, f"ad:bo:{U1}:0")
    view = await adm.press(ADMIN, f"ad:u:{U1}")
    assert buttons(view) == [f"ad:ub:{U1}", "ad:home"]
    out = await adm.press(ADMIN, f"ad:ub:{U1}")
    assert t.ADMIN_UNBANNED.format(status="active") in texts(out)
    assert (await adm.send(U1, "/chat"))[0].text == t.SEARCHING
    assert await app_pool.fetchval("SELECT count(*) FROM admin_actions") == 2


async def test_admin_cannot_ban_self_or_another_admin(adm, app_pool):
    out = await adm.press(ADMIN, f"ad:bo:{ADMIN}:0")
    assert texts(out) == [t.ADMIN_PROTECTED]
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = $1", ADMIN) == "active"


async def test_ban_and_unban_of_ineligible_accounts_are_refused(adm, app_pool):
    await adm.send(U1, "/start")
    await adm.press(U1, "age:no")  # underage block
    assert texts(await adm.press(ADMIN, f"ad:bo:{U1}:0")) == [t.ADMIN_BAD_STATE]
    assert texts(await adm.press(ADMIN, f"ad:ub:{U1}")) == [t.ADMIN_BAD_STATE]
    assert (
        await app_pool.fetchval("SELECT status FROM users WHERE id = $1", U1) == "blocked_underage"
    )
    assert texts(await adm.press(ADMIN, "ad:bo:123456:0")) == [t.ADMIN_NO_USER]


# ---------------- reports ----------------


async def test_report_list_view_and_context(adm, app_pool):
    await adm.onboard(U1)
    await adm.onboard(U2)
    ctx = [{"from": "reported", "text": "hi <b>there</b>"}]
    rid = await report(app_pool, U1, U2, context=ctx, reason="harassment")
    out = await adm.press(ADMIN, "ad:rl:0")
    assert f"ad:r:{rid}" in buttons(out) and "Open reports" in texts(out)[0]
    view = await adm.press(ADMIN, f"ad:r:{rid}")
    text = texts(view)[0]
    assert f"Report #{rid}" in text and "harassment" in text
    assert f"Reporter: {U1}" in text and f"Reported: {U2}" in text
    assert "hi &lt;b&gt;there&lt;/b&gt;" in text  # context shown, HTML-escaped
    assert buttons(view) == [
        f"ad:rv:{rid}",
        f"ad:rd:{rid}",
        f"ad:b:{U2}:{rid}",
        f"ad:u:{U2}",
        "ad:rl:0",
    ]


async def test_empty_report_queue(adm):
    assert texts(await adm.press(ADMIN, "ad:rl:0")) == [t.ADMIN_NO_REPORTS]
    assert texts(await adm.press(ADMIN, "ad:r:12345")) == [t.ADMIN_NO_REPORT]


async def test_resolve_dismiss_and_ban_from_a_report(adm, app_pool):
    for uid in (U1, U2):
        await adm.onboard(uid)
    r1 = await report(app_pool, U1, U2)
    r2 = await report(app_pool, U2, U1)
    out = await adm.press(ADMIN, f"ad:rv:{r1}")
    assert t.ADMIN_REPORT_DONE in texts(out)
    assert await app_pool.fetchval("SELECT status FROM reports WHERE id = $1", r1) == "actioned"
    out = await adm.press(ADMIN, f"ad:rv:{r1}")  # double click
    assert texts(out) == [t.ADMIN_ALREADY]
    await adm.press(ADMIN, f"ad:rd:{r2}")
    assert await app_pool.fetchval("SELECT status FROM reports WHERE id = $1", r2) == "dismissed"
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = $1", U1) == "active"

    r3 = await report(app_pool, U1, U2)
    confirm = await adm.press(ADMIN, f"ad:b:{U2}:{r3}")
    assert buttons(confirm) == [f"ad:bo:{U2}:{r3}", f"ad:r:{r3}"]
    await adm.press(ADMIN, f"ad:bo:{U2}:{r3}")
    assert await app_pool.fetchval("SELECT status FROM users WHERE id = $1", U2) == "banned"
    assert await app_pool.fetchval("SELECT status FROM reports WHERE id = $1", r3) == "actioned"
    actions = [
        r["action"] for r in await app_pool.fetch("SELECT action FROM admin_actions ORDER BY id")
    ]
    assert actions == ["report_resolve", "report_dismiss", "report_ban"]


async def test_a_handled_report_shows_no_action_buttons(adm, app_pool):
    await adm.onboard(U1)
    rid = await report(app_pool, ADMIN, U1)
    await adm.press(ADMIN, f"ad:rd:{rid}")
    view = await adm.press(ADMIN, f"ad:r:{rid}")
    assert buttons(view) == ["ad:home"]


async def test_report_list_paginates(adm, app_pool):
    await adm.onboard(U1)
    for n in range(7):  # one open profile report per (reporter, reported) pair
        await make_user(app_pool, 8000 + n)
        await report(app_pool, 8000 + n, U1)
    first = await adm.press(ADMIN, "ad:rl:0")
    assert "page 1/2" in texts(first)[0] and "ad:rl:1" in buttons(first)
    second = await adm.press(ADMIN, "ad:rl:1")
    assert "page 2/2" in texts(second)[0] and "ad:rl:0" in buttons(second)
    clamped = await adm.press(ADMIN, "ad:rl:50")
    assert "page 2/2" in texts(clamped)[0]


# ---------------- hostile input ----------------


@pytest.mark.parametrize(
    "data",
    [
        "ad:",
        "ad:zzz",
        "ad:u:abc",
        "ad:u:-1",
        "ad:u:1:2:3",
        "ad:bo",
        "ad:ub",
        "ad:u:" + "9" * 17,
        "ad:u:9999999999999999",
        "ad:rl:-1",
        "ad:r:0",
        "AD:home",
        "ad:home ",
        "ad:b:1; DROP TABLE users:0",
    ],
)
async def test_hostile_admin_payloads_do_nothing(adm, app_pool, data):
    await adm.onboard(U1)
    out = await adm.press(ADMIN, data)
    assert not any(o.kind == "SendMessage" for o in out if o.chat_id != ADMIN)
    assert await app_pool.fetchval("SELECT count(*) FROM admin_actions") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM users WHERE status <> 'active'") == 0


async def test_admin_actions_are_rate_limited(adm, app_pool):
    adm.auto_advance = 0.1
    results = []
    for _ in range(14):
        out = await adm.press(ADMIN, "ad:ub:123456")
        results += texts(out)
    assert t.ADMIN_BUSY in results


# ---------------- no regression ----------------


async def test_normal_users_menu_and_commands_are_unchanged(tg):
    out = await tg.onboard(U1)
    assert out[-1].text == t.PROFILE_SAVED
    assert (await tg.send(U1, "/help"))[0].text == t.HELP
    assert "/admin" not in t.HELP  # not advertised to normal users
