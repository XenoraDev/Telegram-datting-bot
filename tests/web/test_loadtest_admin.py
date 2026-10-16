"""The Server Capacity / Load Testing pages: security, the three start layers, reports."""

import json
import re

import pytest
from httpx import ASGITransport, AsyncClient

from bot.db import repo_loadtest as repo
from bot.loadtest.config import TestConfig
from bot.loadtest.evaluate import DEFAULT_THRESHOLDS
from bot.loadtest.safety import LoadTestSettings
from tests.handlers.harness import FakeTelegram
from tests.loadtest.conftest import env_for, mark_database
from tests.web.conftest import ADMIN, DOMAIN, OTHER_ADMIN, SECRET, TOKEN, Web
from web.app.config import load_web_settings
from web.app.main import create_app

FORM = {
    "profile": "custom",
    "chats": "50",
    "msgs_per_chat_min": "40",
    "duration_min": "1",
    "ramp_s": "10",
}
STRANGER = 5551234


def web_env(dsn, **over):
    env = env_for(dsn, BOT_TOKEN=TOKEN, LOAD_TEST_BOT_ID="123456", ADMIN_DOMAIN=DOMAIN)
    env |= {
        "ADMIN_IDS": f"{ADMIN},{OTHER_ADMIN}",
        "LOAD_TEST_ALLOWED_DOMAINS": DOMAIN,
        "ADMIN_BOT_USERNAME": "my_test_bot",
        "ADMIN_SESSION_SECRET": SECRET,
    }
    return env | over


@pytest.fixture
async def tgw(app_pool, dsn):
    """Web + fake Telegram sharing one database, in a correctly declared test environment."""
    from tests.handlers.harness import FakeClock

    await mark_database(app_pool, dsn)
    settings = load_web_settings(web_env(dsn))
    lt = LoadTestSettings.from_env(web_env(dsn))
    async with FakeTelegram(app_pool, loadtest_settings=lt) as tg:
        await tg.onboard(ADMIN)
        clock = FakeClock()
        app = create_app(settings, pool=app_pool, bot=tg.bot, wall_clock=clock, mono_clock=clock)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url=f"https://{DOMAIN}"
        ) as client:
            web = Web(app, client, clock, app_pool)
            await web.login()
            web.tg = tg
            yield web


async def page(web, path="/admin/loadtest"):
    r = await web.client.get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.text


async def confirmation_token(web):
    msg = [o for o in web.tg.inbox(ADMIN) if "LOAD TEST CONFIRMATION" in o.text][-1]
    return msg.markup.inline_keyboard[0][0].callback_data.split(":", 2)[2]


def window(i, chats):
    return {
        "seq": i, "elapsed_s": i * 5.0, "active_chats": chats, "target_msgs_s": 208.3,
        "msgs_s": 200.0 + i, "sent_ok": 1000, "sent_failed": 0, "p50_ms": 40.0,
        "p95_ms": 150.0 + i, "p99_ms": 250.0, "max_ms": 500.0, "cpu_pct": 50.0 + i,
        "ram_pct": 40.0, "swap_pct": 0.0, "db_latency_ms": 3.0,
    }  # fmt: skip


LIVE_WINDOW = {
    "seq": 1, "elapsed_s": 30.0, "active_chats": 20, "target_msgs_s": 33.3, "msgs_s": 31.5,
    "sent_ok": 150, "sent_failed": 2, "p50_ms": 12.0, "p95_ms": 45.0, "p99_ms": 80.0,
    "max_ms": 120.0, "cpu_pct": 33.0, "ram_pct": 41.0, "swap_pct": 0.0, "worker_cpu_pct": 55.0,
    "in_flight": 3, "db_connections": 14, "db_conn_pct": 14.0, "db_latency_ms": 2.5,
    "bot_ok": True, "web_ok": None,
}  # fmt: skip


async def finished_run(pool, chats=250, result="PASS", *, status="completed", **summary):
    cfg = TestConfig("custom", chats, 50, 300, 60)
    rid = await repo.create_run(pool, ADMIN, cfg.as_json(), cfg.digest, DEFAULT_THRESHOLDS)
    base = {
        "ok": 50_000, "failed": 0, "error_rate_pct": 0.02, "p50_ms": 40.0, "p95_ms": 180.0,
        "p99_ms": 300.0, "max_ms": 900.0, "cpu_peak": 67.0, "cpu_p95": 60.0, "cpu_avg": 45.0,
        "ram_peak": 54.0, "swap_peak": 0.0, "db_conn_peak": 20.0, "db_latency_p95": 4.0,
        "throughput_pct": 99.5, "restarts": 0, "restart_detail": [], "db_errors": 0,
        "msgs_s_avg": 200.0, "msgs_s_peak": 215.0, "bot_observed": False, "web_observed": False,
        "rejected": {},
    }  # fmt: skip
    await pool.execute(
        "UPDATE load_test_runs SET status = $2, result = $3, summary = $4::jsonb, "
        "finished_at = now(), started_at = now() - interval '6 minutes', "
        'server_info = \'{"cpu_count": 4, "memory_total_mb": 8192, "postgres": "16.4"}\', '
        'commit_hash = \'abc1234\', environment = \'[{"label": "Environment", "ok": true, '
        '"detail": "fine", "key": "environment_flag"}]\', '
        'result_reasons = \'[{"level": "WARNING", "code": "x", "text": "cpu high"}]\' '
        "WHERE id = $1",
        rid,
        status,
        result,
        json.dumps(base | summary),
    )
    for i in range(1, 6):
        await repo.insert_metric(pool, rid, window(i, chats))
    return rid


# ------------------------------------------------------------------ layer 1: admin authentication


async def test_the_pages_need_an_admin_session(web):
    for path in (
        "/admin/loadtest",
        "/admin/loadtest/1",
        "/admin/loadtest/1/export.csv",
        "/admin/loadtest/compare?a=1&b=2",
    ):
        r = await web.client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    for path in (
        "/admin/loadtest/review",
        "/admin/loadtest/confirmation",
        "/admin/loadtest/1/stop",
        "/admin/loadtest/thresholds",
    ):
        r = await web.post(path, FORM, with_csrf=False)
        assert r.status_code in (303, 403), path
        assert not (200 <= r.status_code < 300)


async def test_someone_who_is_not_an_admin_cannot_log_in_or_reach_it(web):
    r = await web.client.get("/login/callback", params=web.signed(STRANGER))
    assert r.status_code in (303, 400, 403)
    assert not web.sid or (await web.client.get("/admin/loadtest")).status_code == 303
    assert (await web.client.get("/admin/loadtest")).status_code == 303


async def test_every_post_needs_csrf_and_our_own_origin(tgw):
    for path in (
        "/admin/loadtest/review",
        "/admin/loadtest/confirmation",
        "/admin/loadtest/thresholds",
        "/admin/loadtest/1/stop",
        "/admin/loadtest/confirmation/withdraw",
    ):
        assert (await tgw.post(path, FORM, with_csrf=False)).status_code == 403, path
        assert (
            await tgw.post(path, FORM, headers={"Origin": "https://evil.example"})
        ).status_code == 403, path
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_confirmations") == 0


async def test_downloads_are_not_available_to_the_logged_out(tgw):
    rid = await finished_run(tgw.pool)
    ok = await tgw.client.get(f"/admin/loadtest/{rid}/export.csv")
    assert ok.status_code == 200
    await tgw.client.post("/logout", data={"csrf_token": tgw.token()})
    tgw.client.cookies.clear()
    for kind in ("csv", "json"):
        r = await tgw.client.get(f"/admin/loadtest/{rid}/export.{kind}")
        assert r.status_code == 303 and "windows" not in r.text


# ------------------------------------------------------------------ safety status


async def test_a_test_environment_shows_green_and_enables_the_start(tgw):
    html = await page(tgw)
    for needle in (
        "TESTING",
        "TEST DATABASE VERIFIED",
        "REAL TELEGRAM TRAFFIC DISABLED",
        "PROTECTED",
        "Never run load testing against production",
    ):
        assert needle in html, needle
    assert "NOT PROTECTED" not in html and "NOT VERIFIED" not in html
    r = await tgw.post("/admin/loadtest/review", FORM)
    assert r.status_code == 200 and "Send Confirmation to Telegram" in r.text
    button = re.search(r"<button[^>]*>Send Confirmation to Telegram", r.text).group(0)
    assert "disabled" not in button


async def test_an_unproven_environment_shows_red_and_disables_the_start(admin):
    html = (
        await admin.client.get("/admin/loadtest")
    ).text  # the default web fixture declares no test env
    assert "LOAD TEST BLOCKED" in html and "NOT PROTECTED" in html
    r = await admin.post("/admin/loadtest/review", FORM)
    assert re.search(r"<button[^>]*disabled[^>]*>Send Confirmation to Telegram", r.text)
    sent = await admin.post("/admin/loadtest/confirmation", FORM)
    assert sent.status_code == 303 and "msg=lt_blocked" in sent.headers["location"]
    assert await admin.pool.fetchval("SELECT count(*) FROM load_test_confirmations") == 0
    assert await admin.pool.fetchval("SELECT count(*) FROM load_test_runs") == 0
    assert "blocked" in [
        r["action"] for r in await admin.pool.fetch("SELECT action FROM load_test_audit")
    ]


@pytest.mark.parametrize(
    "over",
    [
        {"APP_ENV": "production"},
        {"LOAD_TEST_ENABLED": "0"},
        {"LOAD_TEST_DB_NAME": "x_test"},
        {"LOAD_TEST_ALLOWED_DOMAINS": "else.example.com"},
    ],
)
async def test_each_failed_check_blocks_the_web_start(app_pool, dsn, over):
    await mark_database(app_pool, dsn)
    settings = load_web_settings(web_env(dsn, **over))
    from tests.handlers.harness import FakeClock

    clock = FakeClock()
    app = create_app(settings, pool=app_pool, wall_clock=clock, mono_clock=clock)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url=f"https://{DOMAIN}"
    ) as client:
        web = Web(app, client, clock, app_pool)
        await web.login()
        r = await web.post("/admin/loadtest/confirmation", FORM)
        assert "msg=lt_blocked" in r.headers["location"]
        assert await app_pool.fetchval("SELECT count(*) FROM load_test_confirmations") == 0


# ------------------------------------------------------------------ configuration


async def test_presets_fill_the_form_and_show_the_calculated_load(tgw):
    cases = {"light": (50, 40), "medium": (100, 40), "heavy": (250, 50), "stress": (500, 50)}
    for name, (chats, rate) in cases.items():
        html = await page(tgw, f"/admin/loadtest?preset={name}")
        assert (
            f'name="chats" value="{chats}"' in html
            and f'name="msgs_per_chat_min" value="{rate}"' in html
        )
        assert f"{chats * rate:,} msg/min" in html
    html = await page(tgw, "/admin/loadtest?preset=heavy")
    assert "12,500 msg/min" in html and "208.33 msg/sec" in html and "62,500 at full load" in html


async def test_unknown_presets_are_rejected(tgw):
    assert (await tgw.client.get("/admin/loadtest?preset=nuclear")).status_code == 404


@pytest.mark.parametrize(
    "bad",
    [
        {"chats": "0"},
        {"chats": "-3"},
        {"chats": "x"},
        {"chats": "999999"},
        {"msgs_per_chat_min": "0"},
        {"msgs_per_chat_min": "100000"},
        {"duration_min": "0"},
        {"duration_min": "9999"},
        {"ramp_s": "-1"},
        {"ramp_s": "99999"},
        {"chats": "2000", "msgs_per_chat_min": "100"},
    ],
)
async def test_dangerous_values_are_rejected_and_nothing_is_created(tgw, bad):
    for path in ("/admin/loadtest/review", "/admin/loadtest/confirmation"):
        r = await tgw.post(path, FORM | bad)
        assert r.status_code == 400, (path, bad)
        assert 'role="alert"' in r.text
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_confirmations") == 0
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_runs") == 0


async def test_unknown_form_fields_cannot_smuggle_other_settings(tgw):
    r = await tgw.post(
        "/admin/loadtest/confirmation",
        FORM | {"APP_ENV": "testing", "max_chats": "99999", "token": "x"},
    )
    assert r.status_code == 303
    cfg = json.loads(await tgw.pool.fetchval("SELECT config FROM load_test_confirmations"))
    assert set(cfg) == {"profile", "chats", "msgs_per_chat_min", "duration_s", "ramp_s"}


# ------------------------------------------------------------------ layers 2+3: review, Telegram


async def test_the_full_start_protocol_through_web_and_telegram(tgw):
    review = await tgw.post("/admin/loadtest/review", FORM)
    for needle in (
        "TESTING",
        "Concurrent chats",
        "Messages / chat / minute",
        "Estimated total messages",
        "Ramp-up",
        "Test profile",
    ):
        assert needle in review.text
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_runs") == 0

    sent = await tgw.post("/admin/loadtest/confirmation", FORM)
    assert sent.status_code == 303 and "msg=lt_sent" in sent.headers["location"]
    text = [o for o in tgw.tg.inbox(ADMIN) if "LOAD TEST CONFIRMATION" in o.text][-1].text
    assert "Chats: 50" in text and "Duration: 1 minute" in text
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_runs") == 0  # not started yet
    html = await page(tgw)
    assert "Waiting for your confirmation in Telegram" in html

    token = await confirmation_token(tgw)
    await tgw.tg.press(ADMIN, f"lt:y:{token}")
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_runs") == 1
    html = await page(tgw)
    assert "waiting for the worker" in html and "STOP TEST" in html
    actions = [
        r["action"] for r in await tgw.pool.fetch("SELECT action FROM load_test_audit ORDER BY id")
    ]
    assert (
        actions == ["configured", "configured", "confirmation_sent", "confirmed"]
        or "confirmed" in actions
    )
    assert {"configured", "confirmation_sent", "confirmed"} <= set(actions)


async def test_editing_the_configuration_after_sending_invalidates_the_confirmation(tgw):
    await tgw.post("/admin/loadtest/confirmation", FORM)
    token = await confirmation_token(tgw)
    r = await tgw.post("/admin/loadtest/review", FORM | {"chats": "60"})
    assert "invalidated because the configuration changed" in r.text
    await tgw.tg.press(ADMIN, f"lt:y:{token}")
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_runs") == 0


async def test_withdrawing_cancels_the_confirmation(tgw):
    await tgw.post("/admin/loadtest/confirmation", FORM)
    token = await confirmation_token(tgw)
    r = await tgw.post("/admin/loadtest/confirmation/withdraw")
    assert "msg=lt_withdrawn" in r.headers["location"]
    await tgw.tg.press(ADMIN, f"lt:y:{token}")
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_runs") == 0


async def test_only_one_test_can_be_active_at_a_time(tgw):
    await tgw.post("/admin/loadtest/confirmation", FORM)
    await tgw.tg.press(ADMIN, f"lt:y:{await confirmation_token(tgw)}")
    r = await tgw.post("/admin/loadtest/confirmation", FORM)
    assert "msg=lt_busy" in r.headers["location"]
    review = await tgw.post("/admin/loadtest/review", FORM)
    assert "Another load test is already running" in review.text
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_runs") == 1
    assert (
        "🚫 Another load test is already running."
        in (await tgw.client.get("/admin/loadtest?msg=lt_busy")).text
    )


async def test_a_failed_telegram_delivery_is_reported_and_nothing_can_start(tgw):
    tgw.tg.unreachable.add(ADMIN)
    r = await tgw.post("/admin/loadtest/confirmation", FORM)
    assert "msg=lt_telegram" in r.headers["location"]
    assert await tgw.pool.fetchval("SELECT state FROM load_test_confirmations") == "failed"
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_runs") == 0


# ------------------------------------------------------------------ stop


async def test_stop_cancels_a_queued_test_and_asks_a_running_one_to_wind_down(tgw):
    await tgw.post("/admin/loadtest/confirmation", FORM)
    await tgw.tg.press(ADMIN, f"lt:y:{await confirmation_token(tgw)}")
    rid = await tgw.pool.fetchval("SELECT id FROM load_test_runs")
    r = await tgw.post(f"/admin/loadtest/{rid}/stop")
    assert "msg=lt_cancelled" in r.headers["location"]
    assert await tgw.pool.fetchval("SELECT status FROM load_test_runs") == "cancelled"
    again = await tgw.post(f"/admin/loadtest/{rid}/stop")
    assert "msg=lt_not_active" in again.headers["location"]
    # a running one
    await tgw.post("/admin/loadtest/confirmation", FORM)
    await tgw.tg.press(ADMIN, f"lt:y:{await confirmation_token(tgw)}")
    rid2 = await tgw.pool.fetchval("SELECT id FROM load_test_runs WHERE status = 'queued'")
    await tgw.pool.execute(
        "UPDATE load_test_runs SET status = 'running', heartbeat_at = now() WHERE id = $1", rid2
    )
    r = await tgw.post(f"/admin/loadtest/{rid2}/stop")
    assert "msg=lt_stop" in r.headers["location"]
    assert (
        await tgw.pool.fetchval("SELECT status FROM load_test_runs WHERE id = $1", rid2)
        == "stopping"
    )


# ------------------------------------------------------------------ live dashboard


async def test_the_live_dashboard_shows_target_actual_latency_server_db_and_application(tgw):
    await tgw.post("/admin/loadtest/confirmation", FORM)
    await tgw.tg.press(ADMIN, f"lt:y:{await confirmation_token(tgw)}")
    rid = await tgw.pool.fetchval("SELECT id FROM load_test_runs")
    await tgw.pool.execute(
        "UPDATE load_test_runs SET status = 'running', started_at = now(), heartbeat_at = now()"
    )
    await repo.insert_metric(tgw.pool, rid, LIVE_WINDOW)
    html = await page(tgw)
    for needle in (
        "Target",
        "Actual",
        "Latency",
        "Server",
        "Database",
        "Application",
        "P50",
        "P95",
        "P99",
        "Maximum",
        "CPU",
        "RAM",
        "Swap",
        "Connection usage",
        "Elapsed",
        "Remaining",
        "STOP TEST",
        "Restarts detected",
    ):
        assert needle in html, needle
    assert '<meta http-equiv="refresh" content="5; url=/admin/loadtest">' in html  # live without JS
    assert "45.0 ms" in html and "33.0%" in html and "20" in html


async def test_the_browser_request_never_runs_the_workload(tgw):
    await tgw.post("/admin/loadtest/confirmation", FORM)
    await tgw.tg.press(ADMIN, f"lt:y:{await confirmation_token(tgw)}")
    for _ in range(3):
        await page(tgw)
    assert await tgw.pool.fetchval("SELECT count(*) FROM load_test_metrics") == 0
    assert await tgw.pool.fetchval("SELECT count(*) FROM users WHERE id < 0") == 0
    assert (
        await tgw.pool.fetchval("SELECT status FROM load_test_runs") == "queued"
    )  # only a worker moves it


# ------------------------------------------------------------------ reports


async def test_the_history_lists_the_expected_columns(tgw):
    rid = await finished_run(tgw.pool)
    html = await page(tgw)
    for needle in (
        "Test",
        "Date",
        "Chats",
        "Msg/min/chat",
        "Duration",
        "P95",
        "Peak CPU",
        "Peak RAM",
        "Error rate",
        "Result",
        f"#{rid}",
        "180 ms",
        "67%",
        "54%",
        "0.02%",
        "🟢 PASS",
    ):
        assert needle in html, needle


async def test_the_detailed_report(tgw):
    rid = await finished_run(tgw.pool, 250, "WARNING", cpu_p95=82.0)
    html = await page(tgw, f"/admin/loadtest/{rid}")
    for needle in (
        "Load test #",
        "🟡 WARNING",
        "Why this result",
        "cpu high",
        "250",
        "Messages / chat / minute",
        "Over time",
        "Environment and server",
        "4",
        "8.0 GB",
        "16.4",
        "abc1234",
        "Download CSV",
        "Download JSON",
        "Thresholds this test was judged by",
    ):
        assert needle in html, needle
    assert "<polyline" in html
    assert (await tgw.client.get("/admin/loadtest/999999")).status_code == 404
    assert (await tgw.client.get("/admin/loadtest/0")).status_code == 404


async def test_a_stop_reason_cannot_inject_markup(tgw):
    rid = await finished_run(tgw.pool, status="aborted", result="FAIL")
    await tgw.pool.execute(
        "UPDATE load_test_runs SET partial = true, "
        "stop_reason = '<script>alert(1)</script>' WHERE id = $1",
        rid,
    )
    html = await page(tgw, f"/admin/loadtest/{rid}")
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "partial report" in html


async def test_csv_and_json_export(tgw):
    rid = await finished_run(tgw.pool)
    csv_r = await tgw.client.get(f"/admin/loadtest/{rid}/export.csv")
    assert csv_r.status_code == 200 and csv_r.headers["content-type"].startswith("text/csv")
    assert f'filename="loadtest-{rid}.csv"' in csv_r.headers["content-disposition"]
    lines = csv_r.text.strip().splitlines()
    assert lines[0].startswith("run_id,seq,ts,elapsed_s,active_chats") and len(lines) == 6
    assert lines[1].split(",")[0] == str(rid)
    js = await tgw.client.get(f"/admin/loadtest/{rid}/export.json")
    assert js.status_code == 200 and js.headers["content-type"].startswith("application/json")
    body = js.json()
    assert body["report"]["id"] == rid and body["report"]["result"] == "PASS"
    assert body["report"]["config"]["chats"] == 250 and len(body["windows"]) == 5
    assert body["report"]["server_info"]["cpu_count"] == 4
    assert "exported" in [
        r["action"] for r in await tgw.pool.fetch("SELECT action FROM load_test_audit")
    ]


async def test_exports_contain_no_secrets(tgw):
    rid = await finished_run(tgw.pool)
    for kind in ("csv", "json"):
        text = (await tgw.client.get(f"/admin/loadtest/{rid}/export.{kind}")).text
        for secret in (TOKEN, SECRET, "TEST-TOKEN", "postgresql://", "password", "requested_by"):
            assert secret not in text, (kind, secret)


async def test_only_finished_runs_can_be_exported(tgw):
    await tgw.post("/admin/loadtest/confirmation", FORM)
    await tgw.tg.press(ADMIN, f"lt:y:{await confirmation_token(tgw)}")
    rid = await tgw.pool.fetchval("SELECT id FROM load_test_runs")
    assert (await tgw.client.get(f"/admin/loadtest/{rid}/export.csv")).status_code == 404
    assert (await tgw.client.get("/admin/loadtest/1/export.xml")).status_code == 404
    assert (await tgw.client.get("/admin/loadtest/424242/export.json")).status_code == 404


async def test_historical_comparison(tgw):
    before = await finished_run(tgw.pool, 250, "PASS", p95_ms=220.0, cpu_p95=71.0)
    after = await finished_run(tgw.pool, 250, "PASS", p95_ms=170.0, cpu_p95=54.0)
    html = await page(tgw, f"/admin/loadtest/compare?a={after}&b={before}")  # order does not matter
    assert f"#{before}" in html and f"#{after}" in html
    assert "P95 latency" in html and "220.0 ms" in html and "170.0 ms" in html
    assert "improved" in html and "CPU (95th pct)" in html
    assert (await tgw.client.get("/admin/loadtest/compare?a=1&b=424242")).status_code == 404
    assert (await tgw.client.get("/admin/loadtest/compare?a=x&b=1")).status_code == 404


async def test_comparison_warns_when_workloads_differ(tgw):
    a = await finished_run(tgw.pool, 100)
    b = await finished_run(tgw.pool, 500)
    assert "not directly comparable" in await page(tgw, f"/admin/loadtest/compare?a={a}&b={b}")


# ------------------------------------------------------------------ capacity


async def test_the_capacity_summary_explains_its_choice(tgw):
    await finished_run(tgw.pool, 50)
    await finished_run(tgw.pool, 100)
    await finished_run(tgw.pool, 250, "PASS", p95_ms=180.0, error_rate_pct=0.02)
    await finished_run(tgw.pool, 500, "WARNING", cpu_p95=82.0, p95_ms=620.0)
    await finished_run(tgw.pool, 750, "FAIL", error_rate_pct=5.0)
    html = await page(tgw)
    assert "4 vCPU / 8.0 GB RAM" in html
    assert "250 chats recommended because the 250-chat test passed with 0.02% errors" in html
    assert "500-chat test is the warning zone" in html and "750-chat test failed" in html
    assert "250 concurrent chats" in html and "How the capacity is calculated" in html


async def test_a_server_upgrade_gets_its_own_capacity_line(tgw):
    await finished_run(tgw.pool, 500, "WARNING", cpu_p95=82.0)
    r2 = await finished_run(tgw.pool, 500, "PASS")
    await tgw.pool.execute(
        "UPDATE load_test_runs SET server_info = "
        '\'{"cpu_count": 8, "memory_total_mb": 16384}\' WHERE id = $1',
        r2,
    )
    html = await page(tgw)
    assert "8 vCPU / 16.0 GB RAM" in html and "4 vCPU / 8.0 GB RAM" in html


async def test_run_next_recommended_only_after_a_pass_and_only_prefills(tgw):
    assert "Prepare next recommended test" not in await page(tgw)
    await finished_run(tgw.pool, 100, "PASS")
    html = await page(tgw)
    assert "Prepare next recommended test: 250 chats" in html
    filled = await page(tgw, "/admin/loadtest?next=250")
    assert 'name="chats" value="250"' in filled
    assert (
        await tgw.pool.fetchval("SELECT count(*) FROM load_test_confirmations") == 0
    )  # nothing started
    odd = await page(tgw, "/admin/loadtest?next=999")  # not the recommended step: ignored
    assert (
        'name="chats" value="250"' in odd and 'value="999"' not in odd
    )  # the heavy preset default


async def test_no_next_recommendation_after_a_failure(tgw):
    await finished_run(tgw.pool, 100, "FAIL", status="aborted")
    assert "Prepare next recommended test" not in await page(tgw)


# ------------------------------------------------------------------ thresholds


async def test_thresholds_can_be_edited_and_are_validated(tgw):
    html = await page(tgw)
    assert 'name="p95_warn_ms" value="500"' in html
    form = {k: f"{v:g}" for k, v in DEFAULT_THRESHOLDS.items()}
    bad = await tgw.post("/admin/loadtest/thresholds", form | {"p95_warn_ms": "-1"})
    assert bad.status_code == 400 and "Must be between" in bad.text
    ok = await tgw.post(
        "/admin/loadtest/thresholds", form | {"p95_warn_ms": "250", "p95_fail_ms": "800"}
    )
    assert ok.status_code == 303 and "msg=lt_thresholds" in ok.headers["location"]
    assert 'name="p95_warn_ms" value="250"' in await page(tgw)
    swapped = await tgw.post(
        "/admin/loadtest/thresholds", form | {"cpu_warn_pct": "99", "cpu_fail_pct": "50"}
    )
    assert swapped.status_code == 400


async def test_the_audit_trail_is_shown(tgw):
    await tgw.post("/admin/loadtest/review", FORM)
    html = await page(tgw)
    assert "Audit trail" in html and "configured" in html and str(ADMIN) in html


async def test_the_navigation_links_to_the_module(tgw):
    assert 'href="/admin/loadtest"' in await page(tgw, "/admin/")
