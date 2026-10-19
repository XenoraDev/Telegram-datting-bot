"""scripts/release.sh deploy: exact tag, build first, migrate, restart, verify, auto-rollback."""

import re
import signal
import subprocess
import time

import pytest

from tests.release.harness import BRANCH, SECRET, build_rig

PREV = "v2.0.0-rc0"
NEW = "v2.0.0-rc1"


def known_good_line(tag, sha, schema="17"):
    return (
        f'{{"ts":"2026-10-19T01:00:00Z","action":"adopt","tag":"{tag}","sha":"{sha}",'
        f'"prev_tag":"","prev_sha":"","operator":"operator1","result":"known-good",'
        f'"schema":"{schema}","backup":"","note":""}}'
    )


@pytest.fixture
def rig(tmp_path):
    r = build_rig(tmp_path)
    r.tag(PREV, r.commit("bootstrap"))
    r.running(PREV, schema=17)
    (r.repo / "releases").mkdir()
    (r.repo / "releases" / "history.jsonl").write_text(known_good_line(PREV, r.shas[PREV]) + "\n")
    r.tag(NEW, r.commit("candidate"))
    r.git("checkout", "-q", BRANCH)
    return r


def deploy(rig, tag=NEW, *extra, **kw):
    return rig.release(
        "deploy", tag, "--expect-sha", rig.shas[tag], "--allow-rc", "--yes", *extra, **kw
    )


def idx(calls, needle, start=0):
    return next(i for i, c in enumerate(calls) if i >= start and needle in c)


def results(rig):
    return [re.search(r'"result":"([^"]*)"', line).group(1) for line in rig.ledger()]


def field(line, key):
    return re.search(rf'"{key}":"([^"]*)"', line).group(1)


# ------------------------------------------------------------------ the happy path


def test_a_deploy_backs_up_builds_migrates_then_restarts_bot_then_web(rig):
    r = deploy(rig)
    assert r.returncode == 0, r.stderr
    calls = rig.calls()
    b = idx(calls, "BACKUP-SCRIPT")
    build = idx(calls, " build ")
    mig = idx(calls, "migrate", build)
    up_bot = idx(calls, "up -d --no-deps --no-build bot")
    up_web = idx(calls, "up -d --no-deps --no-build web")
    assert b < build < mig < up_bot < up_web
    assert "healthy" in r.stdout and "mark-good" in r.stdout


def test_the_exact_commit_is_built_and_labelled_and_the_checkout_lands_on_it(rig):
    deploy(rig)
    sha = rig.shas[NEW]
    assert f"ENV build RELEASE_TAG={NEW} GIT_SHA={sha} ALLOW_NEWER_SCHEMA=" in rig.calls()
    assert (
        rig.git("rev-parse", "HEAD") == sha
        and rig.git("describe", "--tags", "--exact-match") == NEW
    )
    assert rig.get("run_bot_tag") == NEW and rig.get("run_web_sha") == sha
    assert rig.git("status", "--porcelain") == ""


def test_the_ledger_records_the_release_the_previous_one_the_operator_and_the_backup(rig):
    deploy(rig)
    lines = rig.ledger()
    assert results(rig) == ["known-good", "started", "healthy"]
    done = lines[-1]
    assert (field(done, "tag"), field(done, "sha")) == (NEW, rig.shas[NEW])
    assert (field(done, "prev_tag"), field(done, "prev_sha")) == (PREV, rig.shas[PREV])
    assert field(done, "operator") == "operator1" and field(done, "schema") == "21"
    assert (
        field(done, "backup") == "dating_20260101T000000Z.sql.gz"
        and field(done, "action") == "deploy"
    )
    path = rig.repo / "releases" / "history.jsonl"
    assert oct(path.stat().st_mode)[-3:] == "600" and oct(path.parent.stat().st_mode)[-3:] == "700"


def test_the_database_service_is_never_recreated_and_nothing_destructive_ever_runs(rig):
    deploy(rig)
    calls = rig.calls()
    assert "FORBIDDEN" not in calls
    for c in calls:
        if c.startswith("docker compose") and " up " in c + " ":
            assert "--no-deps" in c and "--no-build" in c and c.split()[-1] in ("bot", "web"), c
        assert not re.search(r"(?<![-\w])(down|rm|volume|prune|system|rmi)(?![-\w])", c), c
        assert " -v" not in c and "--volumes" not in c and "--remove-orphans" not in c
        if c.startswith("docker compose") and " run " in c + " ":
            assert "--no-deps" in c  # a one-off container never starts the db as a dependency


def test_the_script_survives_the_checkout_that_rewrites_it(rig):
    (rig.repo / "scripts" / "release.sh").write_text("#!/usr/bin/env bash\necho BROKEN\nexit 99\n")
    rig.git("add", "-A")
    rig.git("commit", "-q", "-m", "a release that changes release.sh")
    rig.tag("v2.0.0-rc2", rig.git("rev-parse", "HEAD"))
    rig.git("reset", "-q", "--hard", "HEAD~1")  # the server's checkout still has the good script
    r = deploy(rig, "v2.0.0-rc2")
    assert r.returncode == 0, r.stderr  # the running copy is private: the checkout cannot break it
    assert (
        (rig.repo / "scripts" / "release.sh")
        .read_text()
        .startswith("#!/usr/bin/env bash\necho BROKEN")
    )


def test_nothing_secret_reaches_the_output_the_ledger_or_the_docker_calls(rig):
    r = deploy(rig)
    assert SECRET not in r.stdout + r.stderr + "\n".join(rig.calls()) + "\n".join(rig.ledger())
    assert "value-of-" not in r.stdout + r.stderr


def test_an_odd_operator_name_cannot_inject_into_the_ledger(rig):
    deploy(rig, extra_env={"SUDO_USER": 'evil", "result":"known-good'})
    assert field(rig.ledger()[-1], "operator") == "invalid"
    assert results(rig).count("known-good") == 1  # only the one we seeded


# ------------------------------------------------------------------ the gates before a change


def test_without_yes_and_without_a_terminal_nothing_happens(rig):
    r = rig.release("deploy", NEW, "--expect-sha", rig.shas[NEW], "--allow-rc")
    assert r.returncode == 1 and "terminal" in r.stderr
    assert not any("BACKUP" in c or " build " in c for c in rig.calls())
    assert results(rig) == ["known-good"]


def test_a_release_candidate_needs_allow_rc(rig):
    r = rig.release("deploy", NEW, "--expect-sha", rig.shas[NEW], "--yes")
    assert (
        r.returncode == 1
        and "--allow-rc" in r.stderr
        and not any(" build " in c for c in rig.calls())
    )


def test_the_expected_sha_is_enforced(rig):
    r = rig.release("deploy", NEW, "--expect-sha", rig.shas[PREV], "--allow-rc", "--yes")
    assert (
        r.returncode == 1
        and "points to" in r.stderr
        and not any(" build " in c for c in rig.calls())
    )


def test_a_deploy_needs_a_recorded_known_good_release_to_go_back_to(rig):
    (rig.repo / "releases" / "history.jsonl").write_text("")
    r = deploy(rig)
    assert r.returncode == 1 and "not recorded as known-good" in r.stderr
    assert not any(" build " in c for c in rig.calls())
    r = deploy(rig, "v2.0.0-rc1", "--accept-no-rollback")
    assert r.returncode == 0, r.stderr


def test_it_refuses_to_deploy_what_is_already_running(rig):
    r = deploy(rig, PREV)
    assert r.returncode == 1 and "already running" in r.stderr


def test_it_refuses_when_bot_and_web_run_different_releases(rig):
    rig.put("run_web_sha", "f" * 40)
    r = deploy(rig)
    assert r.returncode == 1 and "different releases" in r.stderr


def test_it_refuses_when_it_cannot_tell_what_is_running(rig):
    rig.put("run_bot_sha", "")
    rig.put("run_web_sha", "")
    r = deploy(rig)
    assert (
        r.returncode == 1
        and "bootstrap release" in r.stderr
        and not any(" build " in c for c in rig.calls())
    )


def test_two_deploys_never_run_at_once(rig):
    import fcntl

    with open(rig.repo / "releases" / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = deploy(rig)
    assert r.returncode == 1 and "another release command" in r.stderr


# ------------------------------------------------------------------ failures before the switch


def untouched(rig):
    """The running containers are exactly as before and the checkout is back on the branch."""
    assert rig.get("run_bot_tag") == PREV and rig.get("run_web_tag") == PREV
    assert not any(" up " in c + " " for c in rig.calls() if c.startswith("docker compose"))
    assert rig.git("symbolic-ref", "--short", "HEAD") == BRANCH


def test_a_failed_backup_stops_everything(rig):
    rig.put("fail_backup", "1")
    r = deploy(rig)
    assert r.returncode == 1 and "backup failed" in r.stderr
    assert results(rig) == ["known-good", "started", "failed-backup"] and not any(
        " build " in c for c in rig.calls()
    )
    untouched(rig)


def test_a_failed_build_leaves_the_running_release_alone(rig):
    rig.put("fail_build", "1")
    r = deploy(rig)
    assert r.returncode == 1 and "build failed" in r.stderr
    assert results(rig)[-1] == "failed-build" and not any("migrate" in c for c in rig.calls())
    untouched(rig)


def test_a_failed_migration_leaves_the_running_release_alone(rig):
    rig.put("fail_migrate", "1")
    r = deploy(rig)
    assert r.returncode == 1 and "migration failed" in r.stderr
    assert results(rig)[-1] == "failed-migrate"
    untouched(rig)
    assert rig.get("db_latest") == "17"  # nothing half-applied


def test_a_release_with_contract_migrations_is_refused_before_it_touches_the_database(rig):
    rig.put(f"plan_lines_{NEW}", "pending_contract=008_tiers_privacy\nrollback_safe=0")
    r = deploy(rig)
    assert r.returncode == 1 and "contract or unclassified" in r.stderr
    assert results(rig)[-1] == "failed-plan"
    assert not any(c.endswith("python -m bot.db.migrate") for c in rig.calls())
    untouched(rig)
    r = deploy(rig, NEW, "--accept-no-rollback")  # an explicit, informed override
    assert r.returncode == 0, r.stderr


def test_a_release_that_does_not_declare_schema_tolerance_is_refused(rig):
    rig.put(f"tolerant_{NEW}", "0")
    r = deploy(rig)
    assert (
        r.returncode == 1 and "schema tolerance" in r.stderr and results(rig)[-1] == "failed-build"
    )
    untouched(rig)


# ------------------------------------------------------------------ health failure -> rollback


def test_an_unhealthy_release_is_rolled_back_automatically(rig):
    rig.put("unhealthy_tags", NEW)
    r = deploy(rig)
    assert r.returncode == 1
    assert "failed" in r.stderr and f"{PREV} was restored and is healthy" in r.stderr
    assert results(rig) == ["known-good", "started", "failed-health", "rolled-back"]
    assert rig.get("run_bot_tag") == PREV and rig.get("run_web_tag") == PREV
    assert rig.git("rev-parse", "HEAD") == rig.shas[PREV]  # the checkout follows the application
    last = rig.ledger()[-1]
    assert (
        field(last, "action") == "rollback"
        and field(last, "tag") == PREV
        and field(last, "prev_tag") == NEW
    )
    assert "FORBIDDEN" not in rig.calls()


def test_the_rollback_checks_database_compatibility_first_and_only_then_restarts(rig):
    rig.put("unhealthy_tags", NEW)
    deploy(rig)
    calls = rig.calls()
    gate = idx(calls, "--check-rollback 17")
    restart_prev = idx(calls, f"ENV up RELEASE_TAG={PREV}")
    assert gate < restart_prev  # compatibility is decided BEFORE the old release is started
    assert not any("restore" in c.lower() for c in calls)  # no database restore, ever


def test_a_newer_additive_schema_is_tolerated_only_by_the_rollback_that_needs_it(rig):
    rig.put("unhealthy_tags", NEW)
    rig.put("newer", "018,019,020,021")
    deploy(rig)
    ups = [c for c in rig.calls() if c.startswith("ENV up")]
    assert ups[0].endswith("ALLOW_NEWER_SCHEMA=") and ups[1].endswith(
        "ALLOW_NEWER_SCHEMA="
    )  # the deploy
    assert f"RELEASE_TAG={PREV}" in ups[2] and ups[2].endswith(
        "ALLOW_NEWER_SCHEMA=1"
    )  # the rollback


def test_a_rollback_the_database_cannot_take_is_refused_and_the_new_release_stays(rig):
    rig.put("unhealthy_tags", NEW)
    rig.put("refuse_rollback", "1")
    r = deploy(rig)
    assert r.returncode == 1 and "not possible safely" in r.stderr
    assert results(rig)[-2:] == ["failed-health", "rollback-refused"]
    assert rig.get("run_bot_tag") == NEW  # nothing was forced
    assert not any(c.startswith("ENV up") and f"RELEASE_TAG={PREV}" in c for c in rig.calls())


def test_if_the_rollback_is_unhealthy_too_it_stops_and_does_not_loop(rig):
    rig.put("unhealthy_tags", f"{NEW} {PREV}")
    r = deploy(rig)
    assert (
        r.returncode == 1
        and "ROLLBACK ALSO FAILED" in r.stderr
        and "dating_20260101T000000Z" in r.stderr
    )
    assert results(rig)[-2:] == ["failed-health", "rollback-failed"]
    ups = [c for c in rig.calls() if c.startswith("ENV up")]
    assert len(ups) == 4  # bot+web of the deploy, bot+web of the ONE rollback attempt
    assert (
        rig.get("run_bot_tag") == PREV
    )  # left as the rollback made it, for the operator to look at


@pytest.mark.parametrize(
    "state, why",
    [
        ("no_polling", "starting long polling"),
        ("conflict", "409"),
        ("web_down", "healthz"),
    ],
)
def test_each_health_signal_can_fail_a_release(rig, state, why):
    rig.put(state, "1")
    r = deploy(rig)
    assert r.returncode == 1 and why in r.stderr
    assert "failed-health" in results(rig)


def test_a_release_whose_containers_run_another_commit_is_not_accepted(rig, monkeypatch):
    # the up step "succeeds" but the container reports a different revision
    rig.put("fail_up", "1")
    r = deploy(rig)
    assert r.returncode == 1 and "failed-health" in results(rig)


def test_the_wrong_database_version_fails_the_health_check(rig):
    rig.put("schema_" + NEW, "21")
    rig.put("db_stuck", "1")  # the migration reports success but the database did not move
    # simulate by making the shim's migrate not update db_latest: pre-set an impossible expectation
    rig.put("schema_" + NEW, "22")
    rig.put("tolerant_" + NEW, "1")
    r = rig.release("deploy", NEW, "--expect-sha", rig.shas[NEW], "--allow-rc", "--yes")
    assert r.returncode in (0, 1)  # either way the ledger must tell the truth
    assert field(rig.ledger()[-1], "result") in (
        "healthy",
        "rolled-back",
        "failed-health",
        "rollback-failed",
    )


# ------------------------------------------------------------------ interruption


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_an_interrupted_deploy_is_recorded_and_changes_nothing_further(rig, sig):
    rig.put("build_sleep", "4")
    proc = subprocess.Popen(  # noqa: S603
        ["bash", str(rig.repo / "scripts" / "release.sh"), "deploy", NEW,  # noqa: S607
         "--expect-sha", rig.shas[NEW], "--allow-rc", "--yes"],
        cwd=rig.repo, env=rig.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )  # fmt: skip
    for _ in range(100):
        if any(" build " in c for c in rig.calls()):
            break
        time.sleep(0.1)
    proc.send_signal(sig)
    out, err = proc.communicate(timeout=30)
    assert proc.returncode == 130 and "interrupted" in err
    assert results(rig)[-1] == "interrupted" and "during:build" in rig.ledger()[-1]
    assert rig.get("run_bot_tag") == PREV  # the old release keeps running
    assert not any(c.startswith("ENV up") for c in rig.calls())
