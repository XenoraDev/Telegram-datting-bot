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
    assert real == {
        "status", "history", "check-tag", "preflight", "deploy", "adopt", "mark-good", "rollback",
    }  # fmt: skip
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


def test_the_runbook_documents_the_annotated_tag_rule_and_its_safe_remediation():
    assert "annotated" in RUNBOOK and "GitHub web UI" in RUNBOOK and "lightweight" in RUNBOOK
    assert "scripts/release.sh check-tag" in RUNBOOK
    assert "never delete first" in RUNBOOK
    assert "Do not re-tag or move it. Investigate the mismatch first." in RUNBOOK
    assert "Do not re-tag or move it. Investigate the mismatch first." in SCRIPT
    assert "--force-with-lease=refs/tags/vX.Y.Z-rcN:<40 hex>" in RUNBOOK
    assert "git fetch --force origin" in RUNBOOK and "git fetch --tags" in RUNBOOK
    section = RUNBOOK.split("**If the tag is lightweight**")[1].split("\n## ")[0]
    for destructive in ("git tag -d", "--delete", "push origin :", "push -d"):
        assert destructive not in section, destructive


def test_the_runbook_states_the_release_branch_and_adopt_before_deploy_requirements():
    assert "release/v2.0.0" in RUNBOOK and "RELEASE_BRANCH=release/v2.0.0" in RUNBOOK
    assert "Without `RELEASE_BRANCH`" in RUNBOOK
    assert "adopt <running tag>" in RUNBOOK and "**before** the first deploy" in RUNBOOK
    assert "is not recorded as known-good" in SCRIPT


def test_the_script_checks_annotation_in_one_place_and_never_edits_tags():
    code = "\n".join(x for x in SCRIPT.splitlines() if not x.lstrip().startswith("#"))
    assert code.count('git cat-file -t "$ref"') == 1  # tag_diagnose is the only classifier
    assert "tag_diagnose" in code and "tag_refusal" in code
    before, _, rest = code.partition("cat <<EOF\n")
    executable = before + rest.split("\nEOF\n", 1)[1]  # without the text of the printed procedure
    for forbidden in ("git tag", "git push", "update-ref", "fetch --force", "tag -d", "--delete"):
        assert forbidden not in executable, forbidden


def test_the_runbook_documents_the_official_tag_creation_workflow():
    workflow = (ROOT / ".github" / "workflows" / "create-release-tag.yml").read_text()
    assert "Create release tag" in RUNBOOK and "create-release-tag.yml" in RUNBOOK
    assert (
        "scripts/create_release_tag.sh" in RUNBOOK
        and (ROOT / "scripts" / "create_release_tag.sh").is_file()
    )
    assert "name: Create release tag" in workflow
    for phrase in (
        "`dry_run`", "branch `release/v2.0.0`", "`expected_sha`", "NOTHING WAS WRITTEN",
        "POST git/tags", "POST git/refs",
        "never moves, force-updates or replaces an **annotated** tag",
        "contents: write", "HTTP 403", "peels to",
    ):  # fmt: skip
        assert phrase in RUNBOOK, phrase
    assert "Create release tag" in README and "annotated" in README


def test_the_runbook_documents_the_exact_lightweight_rc1_replacement_and_its_limits():
    section = RUNBOOK.split("**Replacing the known lightweight `v2.0.0-rc1`")[1].split(
        "**The tag must be"
    )[0]
    for phrase in (
        "74214af5aa2be3a71dc883d5a36b9c6296cc0f37", "69566e3b8ef8b4c64c42c5aa6d69c9421bb12ca7",
        "`replace_lightweight`", "never deployed", "is never replaced, moved or force-updated",
        "reachable from", "need not be the tip", "TEMPORARILY ABSENT", "does not retry",
        "dry run", "**before** anything is deleted",
    ):  # fmt: skip
        assert phrase in section, phrase
    script = (ROOT / "scripts" / "create_release_tag.sh").read_text()
    assert "74214af5aa2be3a71dc883d5a36b9c6296cc0f37" in script and "TEMPORARILY ABSENT" in script
