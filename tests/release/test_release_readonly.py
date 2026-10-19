"""scripts/release.sh: input validation and the commands that change nothing."""

import fcntl

import pytest

from tests.release.harness import BRANCH, SECRET, build_rig

BAD_TAGS = [
    "v2",
    "v2.0",
    "2.0.0",
    "main",
    "HEAD",
    "v2.0.0-beta",
    "v2.0.0-rc",
    "v2.0.0-rc1000",
    "V2.0.0",
    "v2.0.0;id",
    "v2.0.0 && id",
    "$(id)",
    "`id`",
    "../v2.0.0",
    "v2.0.0\nv2.0.1",
    "v99999.0.0",
    "-v2.0.0",
    "v2.0.0-rc1/../..",
    "refs/tags/v2.0.0",
]
GOOD_SHA = "a" * 40


@pytest.fixture
def rig(tmp_path):
    r = build_rig(tmp_path)
    r.tag("v2.0.0-rc1", r.commit("candidate"))
    return r


def preflight(rig, tag="v2.0.0-rc1", sha=None, *extra, **kw):
    return rig.release(
        "preflight", tag, "--expect-sha", sha or rig.shas[tag], "--allow-rc", *extra, **kw
    )


def mutating(rig):
    return [
        c
        for c in rig.calls()
        if any(f" {w} " in c + " " for w in ("build", "up", "run", "stop", "start"))
    ]


# ------------------------------------------------------------------ input validation


@pytest.mark.parametrize("tag", BAD_TAGS)
def test_a_tag_that_is_not_a_release_tag_is_refused_before_anything_runs(rig, tag):
    r = rig.release("preflight", tag, "--expect-sha", GOOD_SHA)
    assert r.returncode == 1
    assert "not a release tag" in r.stderr or "unknown option" in r.stderr  # "-v2.0.0" is an option
    assert rig.calls() == [] and rig.ledger() == []


@pytest.mark.parametrize(
    "sha",
    [
        "",
        "abc",
        "a" * 39,
        "a" * 41,
        "A" * 40,
        "g" * 40,
        "v2.0.0-rc1",
        "HEAD",
        "a" * 40 + ";id",
        "$(id)",
    ],
)
def test_the_expected_sha_must_be_the_full_lowercase_commit_id(rig, sha):
    r = rig.release("preflight", "v2.0.0-rc1", "--expect-sha", sha)
    assert r.returncode == 1 and "40-character" in r.stderr and rig.calls() == []


def test_the_sha_is_required(rig):
    r = rig.release("preflight", "v2.0.0-rc1")
    assert r.returncode == 1 and "40-character" in r.stderr


@pytest.mark.parametrize(
    "args",
    [
        ["preflight", "v2.0.0-rc1", "--bogus"],
        ["preflight", "v2.0.0-rc1", "v2.0.0-rc2"],
        ["preflight", "v2.0.0-rc1", "--expect-sha"],
        ["status", "extra"],
        ["history", "abc"],
        ["history", "99999"],
        ["--help"],
        ["frobnicate"],
        [],
    ],
)
def test_unknown_commands_and_options_are_refused(rig, args):
    r = rig.release(*args)
    assert r.returncode == 1
    assert rig.calls() == [] or all("compose version" in c for c in rig.calls())


def test_a_release_candidate_needs_an_explicit_flag_to_reach_the_server(rig):
    r = rig.release("preflight", "v2.0.0-rc1", "--expect-sha", rig.shas["v2.0.0-rc1"])
    assert r.returncode == 1 and "--allow-rc" in r.stderr


# ------------------------------------------------------------------ preflight (read-only)


def test_preflight_passes_and_changes_nothing(rig):
    head = rig.git("rev-parse", "HEAD")
    r = preflight(rig)
    assert r.returncode == 0, r.stderr
    assert "nothing was changed" in r.stdout
    assert rig.git("rev-parse", "HEAD") == head and rig.git("status", "--porcelain") == ""
    assert mutating(rig) == [] and rig.ledger() == []


def test_the_expected_sha_must_match_what_the_tag_points_to(rig):
    other = rig.commit("later")
    r = preflight(rig, sha=other)
    assert r.returncode == 1 and "points to" in r.stderr


def test_a_moved_tag_is_caught(rig):
    original = rig.shas["v2.0.0-rc1"]
    new = rig.commit("sneaky")
    rig.git("tag", "-f", "-a", "-m", "moved", "v2.0.0-rc1", new)
    rig.git("push", "-q", "-f", "origin", "v2.0.0-rc1")
    r = preflight(rig, sha=original)
    assert r.returncode == 1 and "points to" in r.stderr and rig.ledger() == []


def test_a_lightweight_tag_is_refused(rig):
    rig.tag("v2.0.1", rig.commit("x"), annotated=False)
    r = preflight(rig, "v2.0.1")
    assert r.returncode == 1 and "annotated" in r.stderr


def test_an_unknown_tag_is_refused(rig):
    r = rig.release("preflight", "v9.9.9", "--expect-sha", GOOD_SHA)
    assert r.returncode == 1 and "annotated tag" in r.stderr


def test_a_dirty_working_tree_is_refused(rig):
    (rig.repo / "scripts" / "backup.sh").write_text("# edited on the server\n")
    r = preflight(rig)
    assert r.returncode == 1 and "uncommitted changes" in r.stderr


def test_untracked_files_such_as_the_env_file_do_not_count_as_dirty(rig):
    (rig.repo / "notes.txt").write_text("local note")
    assert preflight(rig).returncode == 0


def test_a_commit_that_is_not_on_the_release_branch_is_refused(rig):
    rig.git("checkout", "-q", "-b", "side")
    side = rig.commit("side-work")
    rig.git("tag", "-a", "-m", "side", "v2.0.2", side)
    rig.git("push", "-q", "origin", "side", "--tags")
    rig.git("checkout", "-q", BRANCH)
    r = preflight(rig, "v2.0.2", side)
    assert r.returncode == 1 and f"not part of origin/{BRANCH}" in r.stderr


def test_a_tag_whose_tree_lacks_the_release_files_is_refused(rig):
    (rig.repo / "release" / "migration_compat.json").unlink()
    rig.git("add", "-A")
    rig.git("commit", "-q", "-m", "drop manifest")
    rig.tag("v2.0.3", rig.git("rev-parse", "HEAD"))
    r = preflight(rig, "v2.0.3")
    assert r.returncode == 1 and "does not contain release/migration_compat.json" in r.stderr


def test_a_missing_env_key_is_named_but_no_value_is_ever_printed(rig):
    text = (
        (rig.repo / ".env")
        .read_text()
        .replace("ADMIN_SESSION_SECRET=value-of-ADMIN_SESSION_SECRET\n", "")
    )
    (rig.repo / ".env").write_text(text)
    r = preflight(rig)
    assert r.returncode == 1 and "ADMIN_SESSION_SECRET" in r.stderr
    assert "value-of-" not in r.stdout + r.stderr and SECRET not in r.stdout + r.stderr


def test_a_missing_env_file_is_refused(rig):
    (rig.repo / ".env").unlink()
    assert "is missing" in preflight(rig).stderr


def test_the_env_file_content_never_reaches_any_output_or_log(rig):
    for args in (("status",), ("history",)):
        out = rig.release(*args)
        assert SECRET not in out.stdout + out.stderr
    out = preflight(rig)
    assert SECRET not in out.stdout + out.stderr
    assert SECRET not in "\n".join(rig.calls()) and all(SECRET not in line for line in rig.ledger())


def test_a_database_that_is_not_healthy_is_refused(rig):
    rig.put("db_unhealthy", "1")
    r = preflight(rig)
    assert r.returncode == 1 and "db container is not healthy" in r.stderr


def test_an_invalid_compose_file_is_refused(rig):
    rig.put("bad_config", "1")
    r = preflight(rig)
    assert r.returncode == 1 and "docker compose config is invalid" in r.stderr


def test_too_little_disk_space_is_refused(rig):
    r = preflight(rig, extra_env={"MIN_FREE_MB": "999999999"})
    assert r.returncode == 1 and "MB free" in r.stderr


def test_two_release_commands_never_run_at_once(rig):
    (rig.repo / "releases").mkdir(exist_ok=True)
    with open(rig.repo / "releases" / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = preflight(rig)
    assert r.returncode == 1 and "another release command is running" in r.stderr


# ------------------------------------------------------------------ status / history


def test_status_reports_what_is_running_and_what_is_checked_out(rig):
    rig.running("v2.0.0-rc1")
    r = rig.release("status")
    assert r.returncode == 0, r.stderr
    sha = rig.shas["v2.0.0-rc1"]
    assert f"bot      : v2.0.0-rc1 {sha}  running/healthy" in r.stdout
    assert f"web      : v2.0.0-rc1 {sha}  running/healthy" in r.stdout
    assert "latest applied migration 21" in r.stdout


def test_status_with_nothing_running_does_not_fail(rig):
    r = rig.release("status")
    assert r.returncode == 0 and "known-good" in r.stdout


def seed(rig, *lines):
    (rig.repo / "releases").mkdir(exist_ok=True)
    (rig.repo / "releases" / "history.jsonl").write_text("\n".join(lines) + "\n")


def line(tag, result, sha="b" * 40, action="deploy", schema="21", prev="v2.0.0-rc0"):
    return (
        f'{{"ts":"2026-10-20T01:00:00Z","action":"{action}","tag":"{tag}","sha":"{sha}",'
        f'"prev_tag":"{prev}","prev_sha":"","operator":"operator1","result":"{result}",'
        f'"schema":"{schema}","backup":"dating_x.sql.gz","note":""}}'
    )


def test_history_prints_the_last_entries(rig):
    seed(rig, *[line(f"v2.0.{i}", "healthy") for i in range(5)])
    out = rig.release("history", "2").stdout.splitlines()
    assert len(out) == 2 and "v2.0.3" in out[0] and "v2.0.4" in out[1]
    assert "healthy" in out[1] and "operator1" in out[1] and "backup=dating_x.sql.gz" in out[1]
    assert len(rig.release("history").stdout.splitlines()) == 5


def test_history_with_no_ledger_is_empty_not_an_error(rig):
    r = rig.release("history")
    assert r.returncode == 0 and r.stdout == ""


def test_the_known_good_list_comes_from_the_ledger_only(rig):
    seed(
        rig,
        line("v2.0.0-rc0", "known-good", "c" * 40, "mark-good", "17"),
        line("v2.0.0-rc1", "healthy", "d" * 40),
        line("v2.0.0-rc2", "failed-health", "e" * 40),
    )
    out = rig.release("status").stdout
    assert f"v2.0.0-rc0 {'c' * 40} 17" in out
    assert "v2.0.0-rc1 " + "d" * 40 not in out.split("recent ledger")[0]
