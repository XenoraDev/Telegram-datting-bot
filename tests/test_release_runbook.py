"""The release runbook stays true to scripts/release.sh and the repository."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = (ROOT / "docs" / "RELEASE_RUNBOOK.md").read_text()
SCRIPT = (ROOT / "scripts" / "release.sh").read_text()
README = (ROOT / "README.md").read_text()


def commands_in_script():
    dispatch = SCRIPT.split("# ---- main ----")[1]
    return set(re.findall(r"^\s+([a-z-]+)\) parse_args", dispatch, re.MULTILINE))


def test_every_subcommand_is_documented_and_every_documented_one_exists():
    real = commands_in_script()
    assert real == {"status", "history", "preflight", "deploy", "adopt", "mark-good", "rollback"}
    documented = set(re.findall(r"scripts/release\.sh ([a-z-]+)", RUNBOOK))
    assert documented == real


def test_every_option_the_runbook_shows_exists_in_the_script():
    options = set(re.findall(r"(--[a-z][a-z-]+)", RUNBOOK)) - {
        "--no-deps", "--no-build", "--profile", "--format", "--detach", "--status", "--info",
        "--plan", "--volumes", "--rm", "--remove-orphans", "--oneline", "--tags", "--exact-match",
        "--entrypoint", "--check-rollback",
    }  # fmt: skip
    for option in options:
        assert option in SCRIPT, option


def test_the_runbook_names_the_forbidden_commands_and_the_script_refuses_them():
    for word in ("docker compose down", "down -v", "restore.sh", "volume", "prune"):
        assert word in RUNBOOK
    assert "-v | --volumes | --remove-orphans | down | rm" in SCRIPT
    code = "\n".join(x for x in SCRIPT.splitlines() if not x.lstrip().startswith("#"))
    assert not re.search(r"compose\s+down|docker\s+volume|docker\s+system|image\s+prune", code)


def test_the_script_never_runs_a_destructive_or_database_restoring_command():
    code = "\n".join(line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#"))
    for forbidden in ("restore.sh", "DROP ", "TRUNCATE", "docker rm", "docker rmi", "rm -rf"):
        assert forbidden not in code.replace('rm -f "$RELEASE_SH_COPY"', ""), forbidden
    assert "eval " not in code


def test_the_documented_versions_and_the_manifest_exist():
    for tag in ("v2.0.0-rc0", "v2.0.0-rc1", "v2.0.0", "v2.0.1", "v2.1.0"):
        assert tag in RUNBOOK
    assert (ROOT / "release" / "migration_compat.json").is_file()
    assert "release/migration_compat.json" in RUNBOOK and "release/migration_compat.json" in README


def test_the_bootstrap_commits_named_in_the_runbook_exist_and_touch_no_migration():
    shas = re.search(r"git cherry-pick (\w+) (\w+)", RUNBOOK).groups()
    check = subprocess.run(  # noqa: S603
        ["git", "rev-parse", "--is-inside-work-tree"],  # noqa: S607
        cwd=ROOT, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if check.returncode != 0:
        pytest.skip("not a git checkout")
    for sha in shas:
        exists = subprocess.run(  # noqa: S603
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],  # noqa: S607
            cwd=ROOT, capture_output=True, check=False,
        )  # fmt: skip
        if exists.returncode != 0:
            pytest.skip("history is not available here")
        files = subprocess.run(  # noqa: S603
            ["git", "show", "--name-only", "--format=", sha],  # noqa: S607
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()  # fmt: skip
        assert not any(f.startswith("migrations/") for f in files), sha  # no migration in it
        assert not any(f.startswith(("release/", "scripts/")) for f in files), sha


def test_the_readme_links_the_runbook_and_states_the_compatibility_policy():
    assert "docs/RELEASE_RUNBOOK.md" in README and "N-1" in README
    assert "ALLOW_NEWER_SCHEMA" in README and "release.sh" in README


def test_the_runbook_does_not_claim_zero_downtime_and_states_the_real_numbers():
    assert (
        "no zero-downtime claim" in RUNBOOK.lower()
        or "There is **no zero-downtime claim**" in RUNBOOK
    )
    assert "10–30 s" in RUNBOOK and "5–15 s" in RUNBOOK


def test_the_runbook_covers_both_sequences():
    for step in ("BACKUP", "VERIFY", "DEPLOY", "MIGRATE", "HEALTH CHECK", "MARK KNOWN-GOOD"):
        assert step in RUNBOOK
    for step in (
        "ROLLBACK",
        "SELECT PREVIOUS VERSION",
        "VERIFY DB COMPATIBILITY",
        "RESTORE APP VERSION",
    ):
        assert step in RUNBOOK


def test_the_runbook_records_the_deployed_bootstrap_as_it_really_is():
    assert "`v2.0.0-rc0`" in RUNBOOK and "5008368b7d637a74d064d51c780b62781d0d8f78" in RUNBOOK
    assert "672982c" in RUNBOOK and "5008368" in RUNBOOK  # the cherry-picked equivalents
    assert "1e259e3" in RUNBOOK and "e71e78a" in RUNBOOK  # of these two
    assert "DONE" in RUNBOOK and "not to be repeated" in RUNBOOK


def test_the_runbook_explains_the_two_lines_and_the_variables_the_script_really_reads():
    for name in ("RELEASE_BRANCH", "RELEASE_REPO", "RELEASE_REMOTE"):
        assert name in RUNBOOK and name in SCRIPT, name
    assert "ancestor of `origin/$RELEASE_BRANCH`" in RUNBOOK
    assert "merge-base --is-ancestor" in SCRIPT


def test_the_020_and_021_release_is_kept_separate_from_the_release_system():
    section = RUNBOOK.split("## 5b.")[1].split("\n## ")[0]
    for commit in ("6cba5e6", "3b99213"):  # the two commits that bring the migrations
        assert commit in section
    assert "Not part of the release-system release" in section
    release_line = RUNBOOK.split("## 5a.")[1].split("## 5b.")[0]
    assert "no migration file" in release_line and "no** migration 020 or 021" in release_line
