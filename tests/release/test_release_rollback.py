"""scripts/release.sh rollback / mark-good / adopt: only recorded known-good releases, DB-gated."""

import re

import pytest

from tests.release.harness import BRANCH, SECRET, build_rig

OLD, MID, NEW = "v2.0.0-rc0", "v2.0.0-rc1", "v2.0.0-rc2"


def entry(action, tag, sha, result, schema="21", prev_tag="", prev_sha=""):
    return (
        f'{{"ts":"2026-10-19T01:00:00Z","action":"{action}","tag":"{tag}","sha":"{sha}",'
        f'"prev_tag":"{prev_tag}","prev_sha":"{prev_sha}","operator":"operator1",'
        f'"result":"{result}","schema":"{schema}","backup":"dating_x.sql.gz","note":""}}'
    )


@pytest.fixture
def rig(tmp_path):
    """rc0, rc1 are known-good; rc2 runs now (deployed and healthy, not yet marked)."""
    r = build_rig(tmp_path)
    r.tag(OLD, r.commit("rc0"))
    r.tag(MID, r.commit("rc1"))
    r.tag(NEW, r.commit("rc2"))
    r.git("checkout", "-q", BRANCH)
    r.running(OLD, schema=17)
    r.running(MID, schema=20)
    r.running(NEW, schema=21)
    (r.repo / "releases").mkdir()
    (r.repo / "releases" / "history.jsonl").write_text(
        "\n".join(
            [
                entry("adopt", OLD, r.shas[OLD], "known-good", "17"),
                entry("deploy", MID, r.shas[MID], "healthy", "20", OLD, r.shas[OLD]),
                entry("mark-good", MID, r.shas[MID], "known-good", "20", OLD, r.shas[OLD]),
                entry("deploy", NEW, r.shas[NEW], "healthy", "21", MID, r.shas[MID]),
            ]
        )
        + "\n"
    )
    return r


def results(rig):
    return [re.search(r'"result":"([^"]*)"', x).group(1) for x in rig.ledger()]


def run_state(rig, tag):
    """Make `tag` the running release (bot and web)."""
    for svc in ("bot", "web"):
        rig.put(f"run_{svc}_tag", tag)
        rig.put(f"run_{svc}_sha", rig.shas[tag])


def rollback(rig, *args, **kw):
    return rig.release("rollback", "--yes", *args, **kw)


def ups(rig):
    return [c for c in rig.calls() if c.startswith("ENV up")]


# ------------------------------------------------------------------ rollback


def test_the_default_rollback_goes_to_the_newest_known_good_release_that_is_not_running(rig):
    run_state(rig, NEW)
    r = rollback(rig)
    assert r.returncode == 0, r.stderr
    assert rig.get("run_bot_tag") == MID and rig.get("run_web_tag") == MID
    assert rig.git("rev-parse", "HEAD") == rig.shas[MID]  # the checkout follows the application
    assert results(rig)[-2:] == ["started", "rolled-back"]
    last = rig.ledger()[-1]
    assert (
        f'"tag":"{MID}"' in last and f'"prev_tag":"{NEW}"' in last and '"action":"rollback"' in last
    )
    assert "rolled back" in r.stdout


def test_the_database_compatibility_is_checked_before_the_old_release_starts(rig):
    run_state(rig, NEW)
    rollback(rig)
    calls = rig.calls()
    gate = next(
        i for i, c in enumerate(calls) if "--check-rollback 20" in c
    )  # MID's newest migration
    start = next(i for i, c in enumerate(calls) if c.startswith(f"ENV up RELEASE_TAG={MID}"))
    assert gate < start
    # the gate runs on the CURRENT image (it knows the manifest) and nothing is built or restored
    assert f"ENV run RELEASE_TAG={NEW}" in "\n".join(calls)
    assert not any(" build " in c for c in calls if c.startswith("docker compose"))
    assert "FORBIDDEN" not in calls and not any("restore" in c.lower() for c in calls)


def test_a_chosen_target_must_be_a_recorded_known_good_release(rig):
    run_state(rig, NEW)
    r = rollback(rig, "--to", OLD)
    assert r.returncode == 0, r.stderr
    assert rig.get("run_bot_tag") == OLD


@pytest.mark.parametrize(
    "target",
    [
        NEW,  # recorded, but it is the running one
        "v9.9.9",  # a valid tag that was never deployed
        "v2.0.0",  # exists nowhere
        "main",
        "HEAD",
        "refs/tags/v2.0.0-rc0",
        "a" * 40,  # a commit id
        "v2.0.0-rc0;id",
        "$(id)",
        "v2.0.0-rc0 v2.0.0-rc1",
        "",
    ],
)
def test_an_arbitrary_ref_is_never_accepted(rig, target):
    run_state(rig, NEW)
    r = rollback(rig, "--to", target)
    assert r.returncode == 1
    assert rig.get("run_bot_tag") == NEW and ups(rig) == []
    assert results(rig) == ["known-good", "healthy", "known-good", "healthy"]  # nothing recorded


def test_a_git_tag_that_exists_but_was_never_marked_good_is_refused(rig):
    run_state(rig, NEW)
    rig.tag("v2.0.5", rig.commit("never deployed"))
    rig.git("reset", "-q", "--hard", "HEAD~1")
    r = rollback(rig, "--to", "v2.0.5")
    assert r.returncode == 1 and "not a recorded known-good release" in r.stderr


def test_rolling_back_when_the_running_release_is_the_only_known_good_one_is_refused(rig):
    run_state(rig, OLD)
    (rig.repo / "releases" / "history.jsonl").write_text(
        entry("adopt", OLD, rig.shas[OLD], "known-good", "17") + "\n"
    )
    r = rollback(rig)
    assert r.returncode == 1 and "no known-good release is recorded" in r.stderr


def test_without_yes_and_a_terminal_nothing_happens(rig):
    run_state(rig, NEW)
    r = rig.release("rollback")
    assert r.returncode == 1 and "terminal" in r.stderr and ups(rig) == []


def test_a_rollback_the_database_cannot_take_is_refused_and_changes_nothing(rig):
    run_state(rig, NEW)
    rig.put("refuse_rollback", "1")
    r = rollback(rig)
    assert r.returncode == 1
    assert "NOT safe for this database" in r.stderr and "contract migration" in r.stderr
    assert "never rolled back by this script" in r.stderr
    assert results(rig)[-1] == "rollback-refused" and ups(rig) == []
    assert rig.get("run_bot_tag") == NEW and rig.git("symbolic-ref", "--short", "HEAD") == BRANCH


def test_a_newer_schema_needs_the_tolerance_switch_and_a_tolerant_target(rig):
    run_state(rig, NEW)
    rig.put("newer", "021")
    assert rollback(rig).returncode == 0
    assert ups(rig)[0].endswith("ALLOW_NEWER_SCHEMA=1") and ups(rig)[1].endswith(
        "ALLOW_NEWER_SCHEMA=1"
    )


def test_the_switch_is_not_set_when_the_schema_is_not_newer(rig):
    run_state(rig, NEW)
    assert rollback(rig).returncode == 0
    assert all(c.endswith("ALLOW_NEWER_SCHEMA=") for c in ups(rig))


def test_a_target_that_does_not_declare_schema_tolerance_cannot_run_on_a_newer_schema(rig):
    run_state(rig, NEW)
    rig.put("newer", "021")
    rig.put(f"tolerant_{MID}", "0")
    r = rollback(rig)
    assert r.returncode == 1 and "schema tolerance" in r.stderr and ups(rig) == []


def test_missing_images_refuse_the_rollback_unless_rebuild_is_given(rig):
    run_state(rig, NEW)
    (rig.state / "images").write_text(f"bot:{NEW}\nweb:{NEW}\n")  # the old images were removed
    r = rollback(rig)
    assert r.returncode == 1 and "--rebuild" in r.stderr and ups(rig) == []
    r = rollback(rig, "--rebuild")
    assert r.returncode == 0, r.stderr
    build = next(c for c in rig.calls() if c.startswith("ENV build"))
    assert (
        f"RELEASE_TAG={MID}" in build and f"GIT_SHA={rig.shas[MID]}" in build
    )  # the RECORDED commit
    assert rig.get("run_bot_tag") == MID


def test_an_unhealthy_rollback_stops_and_never_loops(rig):
    run_state(rig, NEW)
    rig.put("unhealthy_tags", MID)
    r = rollback(rig)
    assert (
        r.returncode == 1
        and "NOT healthy" in r.stderr
        and "database restore is a separate" in r.stderr
    )
    assert results(rig)[-1] == "rollback-failed"
    assert len(ups(rig)) == 2  # one attempt: bot and web


def test_the_rollback_never_runs_two_at_once(rig):
    import fcntl

    run_state(rig, NEW)
    with open(rig.repo / "releases" / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = rollback(rig)
    assert r.returncode == 1 and "another release command" in r.stderr


def test_a_damaged_ledger_entry_is_not_trusted(rig):
    run_state(rig, NEW)
    text = (rig.repo / "releases" / "history.jsonl").read_text().replace(rig.shas[MID], "zz" * 20)
    (rig.repo / "releases" / "history.jsonl").write_text(text)
    r = rollback(rig, "--to", MID)
    assert r.returncode == 1 and "damaged" in r.stderr and ups(rig) == []


def test_nothing_secret_leaks_from_a_rollback(rig):
    run_state(rig, NEW)
    r = rollback(rig)
    assert SECRET not in r.stdout + r.stderr + "\n".join(rig.calls()) + "\n".join(rig.ledger())


# ------------------------------------------------------------------ mark-good


def test_mark_good_records_a_healthy_running_release_as_a_rollback_target(rig):
    run_state(rig, NEW)
    r = rig.release("mark-good", NEW, "--yes")
    assert r.returncode == 0, r.stderr
    last = rig.ledger()[-1]
    assert '"result":"known-good"' in last and f'"tag":"{NEW}"' in last and '"schema":"21"' in last
    # now it is a target: with MID running, it can be chosen
    run_state(rig, MID)
    assert rollback(rig, "--to", NEW).returncode == 0
    assert rig.get("run_bot_tag") == NEW


def test_mark_good_needs_a_recorded_healthy_deploy(rig):
    run_state(rig, NEW)
    r = rig.release("mark-good", "v2.0.0-rc9", "--yes")
    assert r.returncode == 1 and "no healthy deploy" in r.stderr
    assert results(rig)[-1] == "healthy"


def test_mark_good_needs_the_release_to_be_what_is_running_and_healthy_now(rig):
    run_state(rig, MID)  # rc2 was deployed but rc1 is running
    r = rig.release("mark-good", NEW, "--yes")
    assert r.returncode == 1 and "not what the bot and web are running" in r.stderr
    run_state(rig, NEW)
    rig.put("unhealthy_tags", NEW)
    r = rig.release("mark-good", NEW, "--yes")
    assert r.returncode == 1 and "not healthy" in r.stderr
    assert results(rig)[-1] == "healthy"


def test_mark_good_refuses_a_tag_that_looks_like_anything_else(rig):
    for bad in ("main", "v2", "$(id)", "v2.0.0-rc1;id"):
        assert rig.release("mark-good", bad, "--yes").returncode == 1


def test_mark_good_asks_for_confirmation(rig):
    run_state(rig, NEW)
    assert "terminal" in rig.release("mark-good", NEW).stderr


# ------------------------------------------------------------------ adopt (the first release)


def test_adopt_records_what_is_already_running_as_the_first_known_good_release(rig):
    (rig.repo / "releases" / "history.jsonl").write_text("")
    run_state(rig, OLD)
    r = rig.release("adopt", OLD, "--expect-sha", rig.shas[OLD], "--yes")
    assert r.returncode == 0, r.stderr
    (line,) = rig.ledger()
    assert (
        '"action":"adopt"' in line and '"result":"known-good"' in line and '"schema":"17"' in line
    )


@pytest.mark.parametrize(
    "why", ["wrong_sha", "wrong_tag", "unhealthy", "no_images", "not_tolerant"]
)
def test_adopt_refuses_anything_that_is_not_exactly_what_it_says(rig, why):
    (rig.repo / "releases" / "history.jsonl").write_text("")
    run_state(rig, OLD)
    sha = rig.shas[OLD]
    tag = OLD
    if why == "wrong_sha":
        sha = rig.shas[MID]
    if why == "wrong_tag":
        tag = MID
        sha = rig.shas[MID]
    if why == "unhealthy":
        rig.put("unhealthy_tags", OLD)
    if why == "no_images":
        (rig.state / "images").write_text("")
    if why == "not_tolerant":
        rig.put(f"tolerant_{OLD}", "0")
    r = rig.release("adopt", tag, "--expect-sha", sha, "--yes")
    assert r.returncode == 1 and rig.ledger() == []


def test_adopt_cross_checks_the_git_tag_when_it_exists(rig):
    (rig.repo / "releases" / "history.jsonl").write_text("")
    run_state(rig, OLD)
    for svc in ("bot", "web"):
        rig.put(
            f"run_{svc}_sha", rig.shas[MID]
        )  # the containers run rc1's commit, labelled rc0 ...
    r = rig.release("adopt", OLD, "--expect-sha", rig.shas[MID], "--yes")
    assert r.returncode == 1 and "points to" in r.stderr and rig.ledger() == []


# ------------------------------------------------------------------ the whole story


def test_adopt_deploy_mark_good_then_a_failed_release_returns_to_the_marked_one(rig):
    (rig.repo / "releases" / "history.jsonl").write_text("")
    run_state(rig, OLD)
    assert rig.release("adopt", OLD, "--expect-sha", rig.shas[OLD], "--yes").returncode == 0
    d = rig.release("deploy", MID, "--expect-sha", rig.shas[MID], "--allow-rc", "--yes")
    assert d.returncode == 0, d.stderr
    assert rig.release("mark-good", MID, "--yes").returncode == 0
    rig.put("unhealthy_tags", NEW)
    bad = rig.release("deploy", NEW, "--expect-sha", rig.shas[NEW], "--allow-rc", "--yes")
    assert bad.returncode == 1 and f"{MID} was restored and is healthy" in bad.stderr
    assert rig.get("run_bot_tag") == MID  # back on the previous KNOWN-GOOD release
    h = rig.release("history", "20").stdout
    for word in ("adopt", "failed-health", "rolled-back", "known-good", "healthy"):
        assert word in h
