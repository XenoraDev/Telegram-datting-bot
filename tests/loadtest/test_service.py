import json

import asyncpg
import pytest

from bot.db import repo_loadtest as repo
from tests.loadtest.conftest import ADMIN, FORM, OTHER_ADMIN, STRANGER, mark_database


async def runs(pool):
    return await pool.fetch("SELECT * FROM load_test_runs ORDER BY id")


async def actions(pool):
    return [r["action"] for r in await pool.fetch("SELECT action FROM load_test_audit ORDER BY id")]


async def confirmed_run(lt, form=FORM, admin=ADMIN):
    req = await lt.request_confirmation(admin, form)
    assert req.kind == "ok", req
    res = await lt.confirm(admin, req.token)
    assert res.kind == "ok", res
    return res.run_id, req


# ------------------------------------------------------------------ authorization


async def test_a_non_admin_can_do_nothing(lt, app_pool):
    assert (await lt.review(STRANGER, FORM)).kind == "denied"
    assert (await lt.request_confirmation(STRANGER, FORM)).kind == "denied"
    assert (await lt.stop(STRANGER, 1)).kind == "denied"
    assert (await lt.save_thresholds(STRANGER, {}))[0] == "denied"
    assert await lt.runs(STRANGER) == [] and await lt.live(STRANGER) == {}
    assert await lt.capacity(STRANGER) == [] and await lt.thresholds(STRANGER) == {}
    assert await lt.run(STRANGER, 1) is None and await lt.export(STRANGER, 1, "csv") is None
    assert (
        await lt.compare(STRANGER, 1, 2) is None and await lt.confirmation_state(STRANGER) is None
    )
    assert (await lt.confirm(STRANGER, "x" * 32)).kind == "denied"
    assert (await lt.cancel_confirmation(STRANGER, "x" * 32)).kind == "denied"
    assert await runs(app_pool) == []
    assert await app_pool.fetchval("SELECT count(*) FROM load_test_confirmations") == 0


# ------------------------------------------------------------------ the three layers


async def test_nothing_starts_before_the_telegram_confirmation(lt, app_pool):
    req = await lt.request_confirmation(ADMIN, FORM)
    assert req.kind == "ok" and req.token
    assert await runs(app_pool) == []  # layers 1+2 alone never create a job
    assert (await lt.confirmation_state(ADMIN))["state"] == "pending"
    res = await lt.confirm(ADMIN, req.token)
    rows = await runs(app_pool)
    assert res.kind == "ok" and len(rows) == 1 and rows[0]["status"] == "queued"
    assert rows[0]["requested_by"] == ADMIN
    assert json.loads(rows[0]["config"]) == req.config.as_json()
    assert rows[0]["config_hash"] == req.config.digest


async def test_the_token_is_random_and_only_its_hash_is_stored(lt, app_pool):
    a = await lt.request_confirmation(ADMIN, FORM)
    b = await lt.request_confirmation(ADMIN, FORM)
    assert a.token != b.token and len(a.token) >= 32
    dump = json.dumps(
        [dict(r, **{}) for r in await app_pool.fetch("SELECT * FROM load_test_confirmations")],
        default=str,
    )
    assert a.token not in dump and b.token not in dump


async def test_a_confirmation_is_one_time(lt, app_pool):
    _, req = await confirmed_run(lt)
    again = await lt.confirm(ADMIN, req.token)
    assert again.kind == "used"
    assert len(await runs(app_pool)) == 1


async def test_a_confirmation_expires(lt, app_pool):
    req = await lt.request_confirmation(ADMIN, FORM)
    await app_pool.execute(
        "UPDATE load_test_confirmations SET created_at = now() - interval '10 minutes', "
        "expires_at = now() - interval '5 minutes'"
    )
    assert (await lt.confirmation_state(ADMIN))["state"] == "expired"
    assert (await lt.confirm(ADMIN, req.token)).kind == "expired"
    assert await runs(app_pool) == []
    assert "confirmation_expired" in await actions(app_pool)
    assert (await lt.confirm(ADMIN, req.token)).kind == "used"  # and it stays dead


async def test_the_confirmation_is_valid_for_five_minutes(lt, app_pool):
    await lt.request_confirmation(ADMIN, FORM)
    secs = await app_pool.fetchval(
        "SELECT extract(epoch FROM expires_at - created_at) FROM load_test_confirmations"
    )
    assert 295 <= secs <= 305


async def test_a_token_belongs_to_the_admin_it_was_made_for(lt, app_pool):
    req = await lt.request_confirmation(ADMIN, FORM)
    assert (await lt.confirm(OTHER_ADMIN, req.token)).kind == "denied"
    assert await runs(app_pool) == []
    assert (await lt.confirm(ADMIN, req.token)).kind == "ok"


@pytest.mark.parametrize(
    "junk", ["", "short", "x" * 100, "../../etc/passwd", "a b c d e f g h i j k l m n o p"]
)
async def test_garbage_tokens_are_invalid(lt, app_pool, junk):
    await lt.request_confirmation(ADMIN, FORM)
    assert (await lt.confirm(ADMIN, junk)).kind == "invalid"
    assert (await lt.confirm(ADMIN, "A" * 32)).kind == "invalid"
    assert await runs(app_pool) == []


async def test_changing_the_configuration_invalidates_the_confirmation(lt, app_pool):
    req = await lt.request_confirmation(ADMIN, FORM)
    same = await lt.review(ADMIN, FORM)  # looking at the same config again keeps it
    assert same.kind == "ok" and not same.superseded
    changed = await lt.review(ADMIN, FORM | {"chats": "51"})
    assert changed.kind == "ok" and changed.superseded
    assert (await lt.confirmation_state(ADMIN))["state"] == "superseded"
    assert (await lt.confirm(ADMIN, req.token)).kind == "cancelled"
    assert await runs(app_pool) == []


@pytest.mark.parametrize(
    "change",
    [
        {"chats": "51"},
        {"msgs_per_chat_min": "41"},
        {"duration_min": "2"},
        {"ramp_s": "11"},
        {"profile": "light", "chats": "50", "msgs_per_chat_min": "40"},
    ],
)
async def test_every_configuration_field_invalidates_the_old_token(lt, app_pool, change):
    req = await lt.request_confirmation(ADMIN, FORM)
    assert (await lt.review(ADMIN, FORM | change)).superseded
    assert (await lt.confirm(ADMIN, req.token)).kind == "cancelled"
    assert await runs(app_pool) == []


async def test_an_old_token_cannot_confirm_a_different_configuration(lt, app_pool):
    old = await lt.request_confirmation(ADMIN, FORM)
    new = await lt.request_confirmation(ADMIN, FORM | {"chats": "100"})
    assert (await lt.confirm(ADMIN, old.token)).kind == "cancelled"  # superseded by the new one
    res = await lt.confirm(ADMIN, new.token)
    assert res.kind == "ok"
    assert json.loads((await runs(app_pool))[0]["config"])["chats"] == 100


async def test_a_tampered_stored_configuration_is_refused(lt, app_pool):
    req = await lt.request_confirmation(ADMIN, FORM)
    await app_pool.execute(
        "UPDATE load_test_confirmations SET config = jsonb_set(config, '{chats}', '1000')"
    )
    assert (await lt.confirm(ADMIN, req.token)).kind == "invalid"
    assert await runs(app_pool) == []


async def test_limits_lowered_after_the_request_are_enforced_at_confirmation(lt, app_pool):
    import dataclasses

    req = await lt.request_confirmation(ADMIN, FORM)
    lt.settings = dataclasses.replace(
        lt.settings, limits=dataclasses.replace(lt.settings.limits, max_chats=10)
    )
    assert (await lt.confirm(ADMIN, req.token)).kind == "invalid"
    assert await runs(app_pool) == []


async def test_the_admin_can_cancel_in_telegram(lt, app_pool):
    req = await lt.request_confirmation(ADMIN, FORM)
    assert (await lt.cancel_confirmation(OTHER_ADMIN, req.token)).kind == "denied"
    assert (await lt.cancel_confirmation(ADMIN, req.token)).kind == "cancelled"
    assert (await lt.confirm(ADMIN, req.token)).kind == "cancelled"
    assert await runs(app_pool) == []
    assert "confirmation_cancelled" in await actions(app_pool)


# ------------------------------------------------------------------ environment gate


@pytest.mark.parametrize(
    "over",
    [
        {"APP_ENV": "production"},
        {"APP_ENV": ""},
        {"LOAD_TEST_ENABLED": "0"},
        {"LOAD_TEST_DB_NAME": "somewhere_else_test"},
        {"LOAD_TEST_ENV_ID": "a-different-environment"},
        {"LOAD_TEST_BOT_ID": "1234567"},
        {"LOAD_TEST_ALLOWED_DOMAINS": "other.example.com"},
        {"LOAD_TEST_PRODUCTION_BOT_IDS": "7777777"},
    ],
)
async def test_an_unsafe_environment_blocks_the_request(unsafe, app_pool, dsn, over):
    await mark_database(app_pool, dsn)
    svc = unsafe(**over)
    res = await svc.request_confirmation(ADMIN, FORM)
    assert res.kind == "blocked" and res.safety and not res.safety.ok
    assert await app_pool.fetchval("SELECT count(*) FROM load_test_confirmations") == 0
    assert await runs(app_pool) == []
    assert "blocked" in await actions(app_pool)


async def test_an_unmarked_database_blocks(unsafe, app_pool):
    res = await unsafe().request_confirmation(ADMIN, FORM)  # the database has no marker comment
    assert res.kind == "blocked"
    assert {c.key for c in res.safety.failed()} == {"database_identity"}


async def test_the_environment_is_proved_again_at_the_moment_of_confirming(lt, app_pool, dsn):
    req = await lt.request_confirmation(ADMIN, FORM)
    await app_pool.execute(f'COMMENT ON DATABASE "{dsn.rsplit("/", 1)[1]}" IS NULL')  # noqa: S608
    res = await lt.confirm(ADMIN, req.token)
    assert res.kind == "blocked" and await runs(app_pool) == []
    await mark_database(app_pool, dsn)  # fixed: the same unexpired token works
    assert (await lt.confirm(ADMIN, req.token)).kind == "ok"


async def test_unknown_negative_users_mean_isolation_is_not_proved(lt, app_pool):
    await app_pool.execute("INSERT INTO users (id, status) VALUES (-5, 'onboarding')")
    res = await lt.request_confirmation(ADMIN, FORM)
    assert res.kind == "blocked" and {c.key for c in res.safety.failed()} == {"isolation"}


# ------------------------------------------------------------------ one test at a time


async def test_two_load_tests_cannot_be_active_together(lt, app_pool):
    await confirmed_run(lt)
    review = await lt.review(ADMIN, FORM)
    assert review.busy
    res = await lt.request_confirmation(ADMIN, FORM)
    assert res.kind == "busy"
    assert len(await runs(app_pool)) == 1


async def test_a_token_issued_earlier_cannot_start_a_second_test(lt, app_pool):
    early = await lt.request_confirmation(ADMIN, FORM)
    await repo.create_run(app_pool, OTHER_ADMIN, {"chats": 1}, "a" * 64, {})
    assert (await lt.confirm(ADMIN, early.token)).kind == "busy"
    assert len(await runs(app_pool)) == 1
    assert await app_pool.fetchval("SELECT state FROM load_test_confirmations") == "pending"


async def test_the_database_itself_refuses_a_second_active_run(app_pool):
    await repo.create_run(app_pool, ADMIN, {"chats": 1}, "a" * 64, {})
    with pytest.raises(asyncpg.UniqueViolationError):
        await repo.create_run(app_pool, ADMIN, {"chats": 1}, "b" * 64, {})
    await app_pool.execute("UPDATE load_test_runs SET status = 'cancelled', finished_at = now()")
    assert await repo.create_run(app_pool, ADMIN, {"chats": 1}, "b" * 64, {})  # free again


# ------------------------------------------------------------------ stopping


async def test_a_queued_job_is_cancelled_on_the_spot(lt, app_pool):
    rid, _ = await confirmed_run(lt)
    res = await lt.stop(ADMIN, rid)
    assert res.kind == "ok" and res.status == "cancelled"
    row = (await runs(app_pool))[0]
    assert row["status"] == "cancelled" and row["finished_at"] and row["partial"]
    assert (await lt.stop(ADMIN, rid)).kind == "not_active"


async def test_stopping_a_running_test_asks_the_worker_to_wind_down(lt, app_pool):
    rid, _ = await confirmed_run(lt)
    await app_pool.execute("UPDATE load_test_runs SET status = 'running', started_at = now()")
    res = await lt.stop(OTHER_ADMIN, rid)
    assert res.status == "stopping"
    row = (await runs(app_pool))[0]
    assert row["status"] == "stopping" and row["cancel_requested_by"] == OTHER_ADMIN
    assert row["finished_at"] is None  # the worker finishes it and writes the partial report
    assert (await lt.stop(ADMIN, 99999)).kind == "missing"


# ------------------------------------------------------------------ audit


async def test_every_step_is_audited_with_the_admin_identity(lt, app_pool):
    await lt.review(ADMIN, FORM)
    req = await lt.request_confirmation(ADMIN, FORM)
    await lt.delivered(ADMIN, req.confirmation_id, ADMIN, 77)
    rid, _ = await confirmed_run(lt)
    await lt.stop(ADMIN, rid)
    got = await actions(app_pool)
    for expected in ("configured", "confirmation_sent", "confirmed", "cancelled"):
        assert expected in got, got
    rows = await app_pool.fetch("SELECT admin_id, created_at FROM load_test_audit")
    assert all(r["admin_id"] == ADMIN and r["created_at"] for r in rows)


async def test_the_audit_trail_is_append_only(lt, app_pool):
    await lt.review(ADMIN, FORM)
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await app_pool.execute("UPDATE load_test_audit SET detail = 'x'")
    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await app_pool.execute("DELETE FROM load_test_audit")


async def test_a_failed_delivery_kills_the_confirmation(lt, app_pool):
    req = await lt.request_confirmation(ADMIN, FORM)
    await lt.delivery_failed(ADMIN, req.confirmation_id)
    assert (await lt.confirm(ADMIN, req.token)).kind == "used"
    assert await runs(app_pool) == []
    assert "failed" in await actions(app_pool)


# ------------------------------------------------------------------ thresholds


async def test_thresholds_are_validated_stored_and_snapshotted_into_runs(lt, app_pool):
    from bot.loadtest.evaluate import DEFAULT_THRESHOLDS

    form = {k: str(v) for k, v in DEFAULT_THRESHOLDS.items()}
    kind, errors = await lt.save_thresholds(ADMIN, form | {"p95_warn_ms": "-3"})
    assert kind == "invalid" and "p95_warn_ms" in errors
    assert (await lt.thresholds(ADMIN))["p95_warn_ms"] == 500
    kind, _ = await lt.save_thresholds(ADMIN, form | {"p95_warn_ms": "250", "p95_fail_ms": "900"})
    assert kind == "ok" and (await lt.thresholds(ADMIN))["p95_warn_ms"] == 250
    rid, _ = await confirmed_run(lt)
    snap = json.loads((await runs(app_pool))[0]["thresholds"])
    assert snap["p95_warn_ms"] == 250 and snap["p95_fail_ms"] == 900
    assert "thresholds_saved" in await actions(app_pool)
    assert rid


# ------------------------------------------------------------------ fail-safe: lost worker


async def test_a_run_whose_worker_vanished_is_aborted_with_a_partial_report(lt, app_pool):
    rid, _ = await confirmed_run(lt)
    await app_pool.execute(
        "UPDATE load_test_runs SET status = 'running', started_at = now() - interval '2 minutes', "
        "heartbeat_at = now() - interval '5 minutes'"
    )
    metric = {
        "seq": 1, "elapsed_s": 5.0, "active_chats": 10, "target_msgs_s": 5.0, "msgs_s": 4.0,
        "sent_ok": 20, "sent_failed": 0, "cpu_pct": 30.0, "ram_pct": 40.0, "p95_ms": 20.0,
    }  # fmt: skip
    await repo.insert_metric(app_pool, rid, metric)
    assert await lt.reap_stale() == 1
    row = (await runs(app_pool))[0]
    assert row["status"] == "aborted" and row["partial"] and row["finished_at"]
    assert "worker" in row["stop_reason"] and row["result"] == "FAIL"
    summary = json.loads(row["summary"])
    assert summary["ok"] == 20 and summary["approximate"] is True
    assert "aborted" in await actions(app_pool)
    assert await lt.reap_stale() == 0
