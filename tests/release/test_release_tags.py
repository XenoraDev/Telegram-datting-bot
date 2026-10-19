"""scripts/release.sh: annotated-tag validation (`check-tag`, and the same check inside preflight /
deploy / adopt). Production releases need an ANNOTATED tag whose peeled commit is the expected one;
a lightweight tag is refused with the remediation, and nothing is ever created, moved or deleted."""

import subprocess

import pytest

from tests.release.harness import build_rig

TAG = "v2.0.0-rc1"
MISSING_SHA = "c" * 40


@pytest.fixture
def rig(tmp_path):
    r = build_rig(tmp_path)
    r.tag(TAG, r.commit("candidate"))
    return r


def check(rig, tag=TAG, sha=None, **kw):
    return rig.release("check-tag", tag, "--expect-sha", sha or rig.shas[tag], **kw)


def snapshot(rig):
    """Everything validation must leave alone: refs (local + origin), HEAD, the tree, the ledger."""
    return {
        "local_refs": rig.git("for-each-ref"),
        "origin_refs": rig.git("ls-remote", "origin"),
        "head": rig.git("rev-parse", "HEAD"),
        "status": rig.git("status", "--porcelain"),
        "releases_dir": (rig.repo / "releases").exists(),
        "docker_calls": rig.calls(),
    }


def lightweight_everywhere(rig, tag="v2.0.1"):
    rig.tag(tag, rig.commit("lightweight release"), annotated=False)
    return tag


def mutating(rig):
    return [
        c
        for c in rig.calls()
        if any(f" {w} " in c + " " for w in ("build", "up", "run", "stop", "start"))
    ]


def fields(out):
    return {k.strip(): v.strip() for k, _, v in (line.partition(":") for line in out.splitlines())}


# ------------------------------------------------------------------ accepted


def test_a_valid_annotated_tag_is_reported_and_accepted(rig):
    r = check(rig)
    assert r.returncode == 0, r.stderr
    f = fields(r.stdout)
    sha = rig.shas[TAG]
    assert f["state (local)"] == "annotated" and f["suitable"] == "yes"
    assert f["tag object"] != sha  # an annotated tag is its own object ...
    assert f["tag target"] == f"{sha} (commit)"  # ... that points at the commit
    assert f["peeled commit"] == sha and f["expected commit"] == sha
    assert f["state (remote)"].startswith("annotated") and sha in f["state (remote)"]
    assert "Nothing was changed" in r.stdout


def test_an_annotated_tag_with_the_correct_peeled_target_passes_preflight(rig):
    r = rig.release("preflight", TAG, "--expect-sha", rig.shas[TAG], "--allow-rc")
    assert r.returncode == 0, r.stderr
    assert "nothing was changed" in r.stdout


# ------------------------------------------------------------------ refused


def test_a_lightweight_tag_is_reported_and_refused(rig):
    tag = lightweight_everywhere(rig)
    sha = rig.shas[tag]
    r = check(rig, tag)
    assert r.returncode == 1
    f = fields(r.stdout)
    assert f["state (local)"] == "lightweight" and f["suitable"] == "no"
    assert f["tag object"] == sha and f["peeled commit"] == sha  # it names the commit directly
    assert f["state (remote)"].startswith("lightweight")
    assert "LIGHTWEIGHT" in r.stdout and "annotated" in r.stdout.lower()
    assert "NOT suitable for deployment" in r.stdout


def test_a_lightweight_tag_stops_preflight_and_deploy_before_anything_is_written(rig):
    tag = lightweight_everywhere(rig)
    sha = rig.shas[tag]
    before = snapshot(rig)
    for command in ("preflight", "deploy"):
        r = rig.release(command, tag, "--expect-sha", sha, "--yes")
        assert r.returncode == 1, command
        assert "LIGHTWEIGHT" in r.stderr and "ANNOTATED" in r.stderr, command
        assert "never deletes, moves or" in r.stderr, command
    after = snapshot(
        rig
    )  # preflight / deploy take the lock first, so releases/ may exist: nothing else
    for key in ("local_refs", "origin_refs", "head", "status"):
        assert after[key] == before[key], key
    assert rig.ledger() == [] and rig.compose_calls("build") == [] and rig.compose_calls("up") == []
    assert mutating(rig) == []
    assert not any(c.startswith("BACKUP-SCRIPT") for c in rig.calls())


def test_the_remediation_is_replace_in_one_step_and_never_delete_first(rig):
    tag = lightweight_everywhere(rig)
    r = check(rig, tag)
    text = r.stdout
    assert f'git tag -a -f -m "{tag}" {tag} {rig.shas[tag]}' in text
    assert f"--force-with-lease=refs/tags/{tag}:{rig.shas[tag]} origin refs/tags/{tag}" in text
    assert f"git fetch --force origin refs/tags/{tag}:refs/tags/{tag}" in text
    for destructive in ("git tag -d", "--delete", "push origin :", "push -d", "git tag --delete"):
        assert destructive not in text, destructive
    assert "never delete first" in text.lower()


def test_the_printed_remediation_really_converts_a_lightweight_tag(rig):
    tag = lightweight_everywhere(rig)
    sha = rig.shas[tag]
    steps = [ln.strip() for ln in check(rig, tag).stdout.splitlines() if ln.startswith("  git ")]
    assert len(steps) == 5
    for step in steps:
        done = subprocess.run(  # noqa: S603
            ["bash", "-c", step],  # noqa: S607
            cwd=rig.repo, env=rig.env, capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert done.returncode == 0, (step, done.stderr)
    assert f"{sha}\trefs/tags/{tag}^{{}}" in rig.git("ls-remote", "origin", f"refs/tags/{tag}*")
    assert check(rig, tag).returncode == 0
    assert rig.git("rev-parse", "origin/" + "claude/dating-bot-v2") == rig.git(
        "rev-parse", "claude/dating-bot-v2"
    )  # the branch was not touched


def test_fetch_tags_alone_does_not_repair_a_stale_lightweight_copy_but_fetch_force_does(
    rig, tmp_path
):
    tag = lightweight_everywhere(rig)
    sha = rig.shas[tag]
    server = tmp_path / "server"
    subprocess.run(["git", "clone", "-q", str(rig.remote), str(server)], check=True)  # noqa: S603,S607
    rig.git("tag", "-a", "-f", "-m", tag, tag, sha)
    rig.git("push", "-q", f"--force-with-lease=refs/tags/{tag}:{sha}", "origin", f"refs/tags/{tag}")

    def kind():
        return subprocess.run(  # noqa: S603
            ["git", "cat-file", "-t", f"refs/tags/{tag}"],  # noqa: S607
            cwd=server, capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip

    subprocess.run(["git", "fetch", "-q", "--tags", "origin"], cwd=server, check=False)  # noqa: S603,S607
    assert kind() == "commit"  # still lightweight: that is why the remediation says --force
    subprocess.run(  # noqa: S603
        ["git", "fetch", "-q", "--force", "origin", f"refs/tags/{tag}:refs/tags/{tag}"],  # noqa: S607
        cwd=server, check=True,
    )  # fmt: skip
    assert kind() == "tag"


def test_an_annotated_tag_pointing_to_the_wrong_commit_is_refused(rig):
    other = rig.commit("a different commit")
    r = check(rig, sha=other)
    assert r.returncode == 1
    f = fields(r.stdout)
    assert f["state (local)"] == "annotated" and f["suitable"] == "no"
    assert f["peeled commit"] == rig.shas[TAG] and f["expected commit"] == other
    r = rig.release("preflight", TAG, "--expect-sha", other, "--allow-rc")
    assert r.returncode == 1 and "points to" in r.stderr


def test_a_missing_tag_is_refused(rig):
    r = check(rig, "v9.9.9", MISSING_SHA)
    assert r.returncode == 1
    f = fields(r.stdout)
    assert f["state (local)"] == "missing" and f["suitable"] == "no"
    assert f["peeled commit"] == "-" and f["state (remote)"].startswith("missing")
    r = rig.release("preflight", "v9.9.9", "--expect-sha", MISSING_SHA)
    assert r.returncode == 1 and "annotated tag" in r.stderr


def test_a_tag_of_a_tag_is_not_accepted(rig):
    sha = rig.shas[TAG]
    rig.git("tag", "-a", "-m", "outer", "v2.0.2", TAG)  # annotates the annotated tag object
    r = check(rig, "v2.0.2", sha)
    assert r.returncode == 1
    f = fields(r.stdout)
    assert f["state (local)"] == "annotated" and f["suitable"] == "no"
    assert f["peeled commit"] == sha and "(tag)" in f["tag target"]


def test_a_local_annotated_tag_is_not_enough_when_origin_holds_a_lightweight_one(rig):
    tag = lightweight_everywhere(rig, "v2.0.3")
    sha = rig.shas[tag]
    rig.git("tag", "-d", tag)  # local copy only; origin keeps the lightweight tag
    rig.git("tag", "-a", "-m", tag, tag, sha)
    r = check(rig, tag)
    assert r.returncode == 1
    f = fields(r.stdout)
    assert f["state (local)"] == "annotated" and f["state (remote)"].startswith("lightweight")
    assert f["suitable"] == "no" and "lightweight" in f["reason"]


def test_adopt_refuses_a_lightweight_tag_and_writes_nothing(rig):
    tag = lightweight_everywhere(rig)
    rig.running(tag)
    before = snapshot(rig)
    r = rig.release("adopt", tag, "--expect-sha", rig.shas[tag], "--yes")
    assert r.returncode == 1 and "LIGHTWEIGHT" in r.stderr
    assert rig.ledger() == []
    assert snapshot(rig)["local_refs"] == before["local_refs"]


def test_adopt_still_records_an_annotated_tag_and_a_tag_not_in_this_checkout(rig):
    rig.running(TAG)
    r = rig.release("adopt", TAG, "--expect-sha", rig.shas[TAG], "--yes")
    assert r.returncode == 0, r.stderr
    sha = rig.commit("bootstrap")  # a release whose tag is not in this checkout (the rc0 bootstrap)
    rig.running("v2.0.0-rc0", sha)
    r = rig.release("adopt", "v2.0.0-rc0", "--expect-sha", sha, "--yes")
    assert r.returncode == 0, r.stderr


# ------------------------------------------------------------------ nothing is mutated


def test_check_tag_changes_nothing_in_any_state(rig):
    light = lightweight_everywhere(rig)
    cases = [
        (TAG, rig.shas[TAG]),
        (light, rig.shas[light]),
        ("v9.9.9", MISSING_SHA),
        (TAG, MISSING_SHA),
    ]
    before = snapshot(rig)
    for tag, sha in cases:
        check(rig, tag, sha)
    assert snapshot(rig) == before
    assert not (rig.repo / "releases").exists()  # not even the lock directory
    assert not any("docker" in c for c in rig.calls())


def test_check_tag_needs_a_full_sha_and_a_release_tag_and_takes_no_other_input(rig):
    assert rig.release("check-tag", TAG).returncode == 1
    assert rig.release("check-tag", TAG, "--expect-sha", "abc").returncode == 1
    assert rig.release("check-tag", "main", "--expect-sha", MISSING_SHA).returncode == 1
    assert rig.release("check-tag", TAG, "--expect-sha", rig.shas[TAG], "--bogus").returncode == 1
