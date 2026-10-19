"""scripts/create_release_tag.sh and .github/workflows/create-release-tag.yml.

The script runs against a throw-away repository with a local bare repository as its "origin" and a
fake `gh` that performs the two POSTs on that bare repository (so the read-back is real git). The
fake REFUSES (exit 99, logged as FORBIDDEN) every method except POST. Nothing real is touched.
"""

import os
import re
import subprocess

import pytest
import yaml

from tests.release.harness import BRANCH, ROOT, build_rig

SCRIPT = ROOT / "scripts" / "create_release_tag.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "create-release-tag.yml"
TAG = "v2.0.0-rc2"
OTHER_SHA = "c" * 40

GH = r"""#!/usr/bin/env python3
import os, re, subprocess, sys

REMOTE = os.environ["FAKE_REMOTE"]
LOG = os.environ["SHIM_STATE"] + "/gh.log"
args = sys.argv[1:]
with open(LOG, "a") as f:
    f.write("gh " + " ".join(args) + "\n")


def git(*a, input=None, check=True):
    cmd = ["git", "-C", REMOTE, *a]
    return subprocess.run(cmd, input=input, capture_output=True, text=True, check=check)


def mode(name):
    return os.environ.get("SHIM_" + name)


def forbid():
    with open(LOG, "a") as f:
        f.write("FORBIDDEN\n")
    sys.exit(99)


method = args[args.index("--method") + 1] if "--method" in args else "GET"
endpoint = next((a for a in args if a.startswith("repos/")), "")
if "force" in " ".join(args).lower() or method in ("PATCH", "PUT"):
    forbid()
fields = {}
for i, a in enumerate(args):
    if a == "-f":
        k, _, v = args[i + 1].partition("=")
        fields[k] = v

if method == "GET":  # only: read a tag object back
    m = re.fullmatch(r"repos/[^/]+/[^/]+/git/tags/([0-9a-f]{40})", endpoint)
    if not m or mode("FAIL_READBACK"):
        if not m:
            forbid()
        sys.exit(1)
    obj = git("cat-file", "tag", m.group(1)).stdout.splitlines()[0].split()[1]
    print(obj + " commit")
    sys.exit(0)

if method == "DELETE":  # only: one tag ref, never a branch
    m = re.fullmatch(r"repos/[^/]+/[^/]+/git/refs/tags/(v[0-9.rc-]+)", endpoint)
    if not m:
        forbid()
    if mode("FAIL_DELETE"):
        print("HTTP 422", file=sys.stderr)
        sys.exit(1)
    git("update-ref", "-d", "refs/tags/" + m.group(1))
    sys.exit(0)

if method != "POST":
    forbid()

if endpoint.endswith("/git/tags"):
    if mode("FAIL_TAG_OBJECT"):
        print("HTTP 422", file=sys.stderr)
        sys.exit(1)
    target = os.environ["SHIM_WRONG_PEEL"] if mode("WRONG_PEEL") else fields["object"]
    body = (
        f"object {target}\ntype commit\ntag {fields['tag']}\n"
        f"tagger bot <bot@example.com> 0 +0000\n\n{fields['message']}\n"
    )
    made = git("mktag", input=body).stdout.strip()
    if mode("MOVE_TAG"):  # somebody moves the existing tag right after the object was made
        git("update-ref", "refs/tags/" + fields["tag"], os.environ["SHIM_MOVE_TO"])
    print(made)
    sys.exit(0)

if endpoint.endswith("/git/refs"):
    ref, sha = fields["ref"], fields["sha"]
    if mode("FAIL_REF"):
        print("HTTP 500", file=sys.stderr)
        sys.exit(1)
    if mode("RACE"):  # somebody else creates the tag between the check and this call
        git("update-ref", ref, os.environ["SHIM_RACE"])
    if mode("LIGHTWEIGHT"):  # a ref straight to the commit instead of the tag object
        sha = git("rev-parse", sha + "^{commit}").stdout.strip()
    done = git("update-ref", ref, sha, "", check=False)  # "" = the ref must not exist yet
    if done.returncode != 0:
        print("HTTP 422 Reference already exists", file=sys.stderr)
        sys.exit(1)
    print(ref)
    sys.exit(0)
sys.exit(2)
"""


@pytest.fixture
def rig(tmp_path):
    r = build_rig(tmp_path)
    r.release_sha = r.commit("release candidate")
    r.git("push", "-q", "origin", BRANCH)
    (r.bin / "gh").write_text(GH)
    (r.bin / "gh").chmod(0o755)
    return r


def run_script(rig, *args, extra_env=None):
    env = {
        **rig.env,
        "FAKE_REMOTE": str(rig.remote),
        "GITHUB_REPOSITORY": "XenoraDev/Telegram-datting-bot",
        "RELEASE_BRANCH": BRANCH,
        "GH_TOKEN": "not-a-real-token",
        **(extra_env or {}),
    }
    return subprocess.run(  # noqa: S603
        ["bash", str(SCRIPT), *args],  # noqa: S607
        cwd=rig.repo, env=env, capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip


def create(rig, tag=TAG, sha=None, *extra, **kw):
    return run_script(rig, "--tag", tag, "--expected-sha", sha or rig.release_sha, *extra, **kw)


def gh_calls(rig):
    p = rig.state / "gh.log"
    return p.read_text().splitlines() if p.exists() else []


def remote_tags(rig):
    return rig.git("ls-remote", "--tags", "origin")


def refs_snapshot(rig):
    return {
        "remote": rig.git("ls-remote", "origin"),
        "local": rig.git("for-each-ref"),
        "head": rig.git("rev-parse", "HEAD"),
        "status": rig.git("status", "--porcelain"),
    }


def before_sha(rig, before):
    return next(ln.split()[0] for ln in before["remote"].splitlines() if f"refs/tags/{TAG}" in ln)


def assert_refused_before_any_write(rig, r, before, text=None):
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOTHING WAS WRITTEN" in r.stderr
    if text:
        assert text in r.stderr, r.stderr
    assert gh_calls(rig) == []  # not a single API call
    assert refs_snapshot(rig) == before


# ------------------------------------------------------------------ the happy path


def test_creates_an_annotated_tag_with_the_object_first_then_its_read_back_then_the_ref(rig):
    r = create(rig)
    assert r.returncode == 0, r.stderr
    sha = rig.release_sha
    out = rig.git("ls-remote", "origin", f"refs/tags/{TAG}", f"refs/tags/{TAG}^{{}}")
    ref_line, peeled_line = out.splitlines()
    tag_object = ref_line.split()[0]
    assert tag_object != sha  # the ref names a tag OBJECT ...
    assert peeled_line.split() == [sha, f"refs/tags/{TAG}^{{}}"]  # ... that peels to the commit
    rig.git("fetch", "-q", "--no-tags", "origin", f"refs/tags/{TAG}:refs/check/t")
    assert rig.git("cat-file", "-t", "refs/check/t") == "tag"
    assert rig.git("rev-parse", "refs/check/t^{commit}") == sha
    calls = gh_calls(rig)
    assert len(calls) == 3 and "FORBIDDEN" not in calls  # object, read-back of the object, ref
    assert "--method POST repos/XenoraDev/Telegram-datting-bot/git/tags" in calls[0]
    assert f"api repos/XenoraDev/Telegram-datting-bot/git/tags/{tag_object}" in calls[1]
    assert "--method POST repos/XenoraDev/Telegram-datting-bot/git/refs" in calls[2]
    assert f"-f ref=refs/tags/{TAG}" in calls[2] and f"-f sha={tag_object}" in calls[2]
    assert not any("DELETE" in c for c in calls)
    assert "verified" in r.stdout and "annotated" in r.stdout
    assert "release.sh check-tag" in r.stdout


def test_the_created_tag_is_accepted_by_release_sh_check_tag(rig):
    assert create(rig).returncode == 0
    rig.git("fetch", "-q", "origin", "refs/tags/" + TAG + ":refs/tags/" + TAG)
    r = rig.release("check-tag", TAG, "--expect-sha", rig.release_sha)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "suitable        : yes" in r.stdout


def test_validate_only_writes_nothing_and_makes_no_api_call(rig):
    before = refs_snapshot(rig)
    r = create(rig, TAG, None, "--validate-only")
    assert r.returncode == 0, r.stderr
    assert "dry run: nothing was written or deleted" in r.stdout
    assert gh_calls(rig) == [] and refs_snapshot(rig) == before


# ------------------------------------------------------------------ refused before any write


@pytest.mark.parametrize("annotated", [True, False])
def test_an_existing_tag_on_the_remote_is_refused_and_left_untouched(rig, annotated):
    rig.tag(TAG, rig.release_sha, annotated=annotated)  # pushed to origin
    rig.git("tag", "-d", TAG)  # the checkout does not have it: the REMOTE check must catch it
    before = refs_snapshot(rig)
    assert_refused_before_any_write(rig, create(rig), before, "already exists on origin")
    assert rig.git("ls-remote", "origin", f"refs/tags/{TAG}").split()[0] == before_sha(rig, before)


def test_an_existing_tag_in_the_checkout_is_refused(rig):
    rig.git("tag", "-a", "-m", "local only", TAG, rig.release_sha)
    before = refs_snapshot(rig)
    assert_refused_before_any_write(rig, create(rig), before, "already exists in this checkout")


def test_an_existing_tag_pointing_elsewhere_is_never_moved(rig):
    other = rig.commit("later")
    rig.git("push", "-q", "origin", BRANCH)
    rig.tag(TAG, other, annotated=True)
    rig.git("tag", "-d", TAG)
    before = refs_snapshot(rig)
    r = create(rig, TAG, rig.release_sha)
    assert_refused_before_any_write(rig, r, before, "ANNOTATED")
    assert rig.git("ls-remote", "origin", f"refs/tags/{TAG}^{{}}").split()[0] == other


@pytest.mark.parametrize("sha", [OTHER_SHA, "a" * 39, "A" * 40, "g" * 40, "HEAD", "", "main"])
def test_a_wrong_or_malformed_sha_is_refused(rig, sha):
    before = refs_snapshot(rig)
    r = run_script(rig, "--tag", TAG, "--expected-sha", sha)
    assert_refused_before_any_write(rig, r, before)


def test_a_sha_that_does_not_exist_is_refused_with_its_reason(rig):
    before = refs_snapshot(rig)
    assert_refused_before_any_write(rig, create(rig, TAG, OTHER_SHA), before, "is not a commit")


def test_a_sha_that_is_not_a_commit_is_refused(rig):
    tree = rig.git("rev-parse", "HEAD^{tree}")
    before = refs_snapshot(rig)
    assert_refused_before_any_write(rig, create(rig, TAG, tree), before, "is not a commit")


def test_a_commit_on_another_branch_is_refused(rig):
    rig.git("checkout", "-q", "-b", "side")
    side = rig.commit("not on the release branch")
    rig.git("push", "-q", "origin", "side")
    rig.git("checkout", "-q", BRANCH)
    before = refs_snapshot(rig)
    r = create(rig, TAG, side)
    assert_refused_before_any_write(rig, r, before, "is not part of origin/" + BRANCH)


def test_a_local_commit_that_was_never_pushed_is_refused(rig):
    local_only = rig.commit("local only")
    before = refs_snapshot(rig)
    r = create(rig, TAG, local_only)
    assert_refused_before_any_write(rig, r, before, "is not part of origin/" + BRANCH)


def test_a_commit_older_than_the_tip_but_on_the_branch_is_accepted(rig):
    older = rig.release_sha
    rig.commit("newer")
    rig.git("push", "-q", "origin", BRANCH)
    assert create(rig, TAG, older, "--validate-only").returncode == 0


def test_a_commit_without_the_files_release_sh_needs_is_refused(rig):
    rig.git("rm", "-q", "Dockerfile")
    rig.git("commit", "-q", "-m", "no Dockerfile")
    bad = rig.git("rev-parse", "HEAD")
    rig.git("push", "-q", "origin", BRANCH)
    before = refs_snapshot(rig)
    r = create(rig, TAG, bad)
    assert_refused_before_any_write(rig, r, before, "does not contain Dockerfile")


@pytest.mark.parametrize(
    "tag",
    ["v2", "v2.0", "2.0.0", "main", "HEAD", "v2.0.0-beta", "v2.0.0-rc", "v2.0.0;id", "$(id)",
     "refs/tags/v2.0.0", "-v2.0.0", "v2.0.0\nv2.0.1", "v99999.0.0", ""],
)  # fmt: skip
def test_a_tag_that_is_not_a_release_tag_is_refused(rig, tag):
    before = refs_snapshot(rig)
    r = run_script(rig, "--tag", tag, "--expected-sha", rig.release_sha)
    assert_refused_before_any_write(rig, r, before)


@pytest.mark.parametrize("tag", ["v2.0.0", "v2.0.1", "v2.1.0", "v2.0.0-rc2", "v10.20.30-rc999"])
def test_release_tags_of_every_documented_shape_pass_validation(rig, tag):
    assert create(rig, tag, None, "--validate-only").returncode == 0


def test_unknown_arguments_and_missing_values_are_refused(rig):
    assert run_script(rig, "--bogus").returncode == 1
    assert run_script(rig, "--tag").returncode == 1
    assert run_script(rig).returncode == 1
    assert gh_calls(rig) == []


# ------------------------------------------------------------------ after the first write


def test_verification_catches_a_lightweight_tag_and_changes_nothing_further(rig):
    r = create(rig, extra_env={"SHIM_LIGHTWEIGHT": "1"})
    assert r.returncode == 1 and "VERIFY FAILED" in r.stderr and "lightweight" in r.stderr
    calls = gh_calls(rig)
    assert (
        len(calls) == 3 and "FORBIDDEN" not in calls
    )  # no repair attempt: no delete, no re-create
    assert not any("DELETE" in c for c in calls)
    assert rig.git("ls-remote", "origin", f"refs/tags/{TAG}").split()[0] == rig.release_sha


def test_a_tag_object_that_points_at_another_commit_is_caught_before_any_ref_exists(rig):
    other = rig.commit("other")
    rig.git("push", "-q", "origin", BRANCH)
    r = create(rig, extra_env={"SHIM_WRONG_PEEL": other})
    assert r.returncode == 1 and "expected" in r.stderr and "NOTHING WAS WRITTEN" in r.stderr
    assert len(gh_calls(rig)) == 2  # object, read-back: and no ref
    assert remote_tags(rig) == ""


def test_a_tag_created_by_somebody_else_in_the_meantime_is_not_overwritten(rig):
    rival = rig.commit("rival")
    rig.git("push", "-q", "origin", BRANCH)
    r = create(rig, extra_env={"SHIM_RACE": rival})
    assert r.returncode == 1 and "Nothing was overwritten" in r.stderr
    assert rig.git("ls-remote", "origin", f"refs/tags/{TAG}").split()[0] == rival  # theirs stands
    calls = gh_calls(rig)
    assert len(calls) == 3 and "FORBIDDEN" not in calls


def test_a_refused_tag_object_stops_before_any_ref_is_created(rig):
    r = create(rig, extra_env={"SHIM_FAIL_TAG_OBJECT": "1"})
    assert r.returncode == 1 and "NOTHING WAS WRITTEN" in r.stderr
    assert len(gh_calls(rig)) == 1 and remote_tags(rig) == ""


def test_running_it_twice_never_touches_the_first_tag(rig):
    assert create(rig).returncode == 0
    first = rig.git("ls-remote", "origin", f"refs/tags/{TAG}")
    r = create(rig)
    assert r.returncode == 1 and "already exists" in r.stderr
    assert rig.git("ls-remote", "origin", f"refs/tags/{TAG}") == first
    assert len(gh_calls(rig)) == 3  # the second run made no call at all


def test_the_fake_gh_only_allows_the_calls_the_script_may_make(rig):
    gh = rig.bin / "gh"
    env = {**rig.env, "FAKE_REMOTE": str(rig.remote)}
    forbidden = [
        ["--method", "PATCH", "repos/o/r/git/refs/tags/v1.0.0"],
        ["--method", "PUT", "repos/o/r/git/refs/tags/v1.0.0"],
        ["--method", "DELETE", "repos/o/r/git/refs/heads/main"],  # a branch, never
        ["repos/o/r/git/refs/tags/v1.0.0"],  # a GET of anything but a tag object
        ["--method", "POST", "repos/o/r/git/refs", "-f", "force=true"],
    ]
    for rest in forbidden:
        done = subprocess.run(  # noqa: S603
            [str(gh), "api", *rest], env=env, capture_output=True, text=True, check=False
        )  # fmt: skip
        assert done.returncode == 99, rest
    assert gh_calls(rig).count("FORBIDDEN") == len(forbidden)


# ------------------------------------------------------------------ replacing the known tag

RC1 = "v2.0.0-rc1"


@pytest.fixture
def rc1(rig):
    """origin: v2.0.0-rc1 is LIGHTWEIGHT at OLD; the release branch tip NEW is its descendant.

    The script's allow-list is pointed at these throw-away commits (the workflow cannot do that)."""
    rig.old_sha = rig.commit("the commit rc1 points to (never deployed)")
    rig.tag(RC1, rig.old_sha, annotated=False)  # pushed, lightweight
    rig.new_sha = rig.commit("the release tip")
    rig.git("push", "-q", "origin", BRANCH)
    rig.git("tag", "-d", RC1)  # the server-side checkout of the workflow has it; tests vary this
    rig.allow = {
        "REPLACE_ALLOW_TAG": RC1,
        "REPLACE_ALLOW_OLD_SHA": rig.old_sha,
        "REPLACE_ALLOW_NEW_SHA": rig.new_sha,
    }
    return rig


def replace(rig, *extra, sha=None, tag=RC1, extra_env=None, flag=True):
    args = ["--tag", tag, "--expected-sha", sha or rig.new_sha, *extra]
    if flag:
        args.append("--replace-lightweight")
    return run_script(rig, *args, extra_env={**rig.allow, **(extra_env or {})})


def remote_rc1(rig):
    return rig.git("ls-remote", "origin", f"refs/tags/{RC1}", f"refs/tags/{RC1}^{{}}")


def assert_untouched(rig, r, before, text=None):
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOTHING WAS WRITTEN OR DELETED" in r.stderr
    if text:
        assert text in r.stderr, r.stderr
    assert gh_calls(rig) == []  # no object, no delete, no ref
    assert refs_snapshot(rig) == before


def test_the_exact_known_lightweight_tag_is_replaced_by_an_annotated_one(rig, rc1):
    r = replace(rc1)
    assert r.returncode == 0, r.stderr
    new = rc1.new_sha
    out = rc1.git("ls-remote", "origin", f"refs/tags/{RC1}", f"refs/tags/{RC1}^{{}}").splitlines()
    assert len(out) == 2  # a ref AND a peeled line: it is annotated now
    assert out[1].split() == [new, f"refs/tags/{RC1}^{{}}"]
    assert out[0].split()[0] != new  # the ref names a tag object, not the commit
    rc1.git("fetch", "-q", "--no-tags", "origin", f"refs/tags/{RC1}:refs/check/rc1")
    assert rc1.git("cat-file", "-t", "refs/check/rc1") == "tag"
    assert rc1.git("rev-parse", "refs/check/rc1^{commit}") == new
    assert "mode: replace" in r.stdout and "verified" in r.stdout and "annotated" in r.stdout


def test_the_replacement_order_object_then_readback_then_delete_then_ref(rc1):
    r = replace(rc1)
    assert r.returncode == 0, r.stderr
    calls = gh_calls(rc1)
    assert len(calls) == 4 and "FORBIDDEN" not in calls
    assert "--method POST" in calls[0] and calls[0].split()[4].endswith("/git/tags")
    assert "--method" not in calls[1] and "/git/tags/" in calls[1]  # the object is read back first
    assert f"--method DELETE repos/XenoraDev/Telegram-datting-bot/git/refs/tags/{RC1}" in calls[2]
    assert "--method POST" in calls[3] and calls[3].split()[4].endswith("/git/refs")
    assert not any(w in " ".join(calls) for w in ("PATCH", "PUT", "force"))


def test_the_replaced_tag_is_accepted_by_release_sh_check_tag(rc1):
    assert replace(rc1).returncode == 0
    rc1.git("fetch", "-q", "--force", "origin", f"refs/tags/{RC1}:refs/tags/{RC1}")
    r = rc1.release("check-tag", RC1, "--expect-sha", rc1.new_sha)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "suitable        : yes" in r.stdout


def test_dry_run_checks_everything_and_deletes_and_creates_nothing(rc1):
    before = refs_snapshot(rc1)
    r = replace(rc1, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "mode: replace" in r.stdout and "dry run, would:" in r.stdout
    assert "delete that lightweight ref" in r.stdout
    assert "nothing was written or deleted" in r.stdout
    assert gh_calls(rc1) == [] and refs_snapshot(rc1) == before
    assert remote_rc1(rc1).split()[0] == rc1.old_sha and "^{}" not in remote_rc1(rc1)


def test_dry_run_applies_every_stop_condition_too(rc1):
    before = refs_snapshot(rc1)
    r = replace(rc1, "--dry-run", extra_env={"REPLACE_ALLOW_OLD_SHA": OTHER_SHA})
    assert_untouched(rc1, r, before, "not retargeting an unexpected tag")


def test_without_the_explicit_request_nothing_is_replaced(rc1):
    before = refs_snapshot(rc1)
    assert_untouched(rc1, replace(rc1, flag=False), before, "replace_lightweight")


def test_a_lightweight_tag_at_the_wrong_current_sha_is_not_retargeted(rc1):
    stray = rc1.git("rev-parse", f"{rc1.old_sha}~1")  # a real commit, but not the known old one
    rc1.git(
        "push",
        "-q",
        f"--force-with-lease=refs/tags/{RC1}:{rc1.old_sha}",
        "origin",
        f"{stray}:refs/tags/{RC1}",
    )
    before = refs_snapshot(rc1)
    r = replace(rc1)
    assert_untouched(rc1, r, before, f"at {stray}, not at the known {rc1.old_sha}")
    assert remote_rc1(rc1).split()[0] == stray


def test_an_existing_annotated_tag_is_never_replaced(rc1):
    rc1.git("tag", "-d", RC1, check=False)
    rc1.git("tag", "-a", "-f", "-m", "annotated", RC1, rc1.old_sha)
    rc1.git("push", "-q", "--force", "origin", f"refs/tags/{RC1}")
    rc1.git("tag", "-d", RC1)
    before = refs_snapshot(rc1)
    for extra in ((), ("--dry-run",)):
        r = replace(rc1, *extra)
        assert_untouched(rc1, r, before, "ANNOTATED")
        assert "never replaced, moved, deleted or force-updated" in r.stderr


def test_an_existing_annotated_tag_on_the_expected_commit_is_left_alone(rc1):
    rc1.git("tag", "-a", "-f", "-m", "annotated", RC1, rc1.new_sha)
    rc1.git("push", "-q", "--force", "origin", f"refs/tags/{RC1}")
    rc1.git("tag", "-d", RC1)
    before = refs_snapshot(rc1)
    assert_untouched(rc1, replace(rc1), before, "ANNOTATED")


def test_another_lightweight_tag_is_never_replaced(rc1):
    rc1.tag("v2.0.1", rc1.old_sha, annotated=False)
    rc1.git("tag", "-d", "v2.0.1")
    before = refs_snapshot(rc1)
    r = replace(rc1, tag="v2.0.1")
    assert_untouched(rc1, r, before, f"only {RC1} may be replaced")


def test_a_target_that_is_not_the_requested_release_commit_stops(rc1):
    before = refs_snapshot(rc1)
    for wrong in (rc1.old_sha, rc1.release_sha, OTHER_SHA):
        r = replace(rc1, sha=wrong)
        assert r.returncode == 1 and "NOTHING WAS WRITTEN OR DELETED" in r.stderr, wrong
    assert gh_calls(rc1) == [] and refs_snapshot(rc1) == before


def test_the_target_must_be_exactly_the_tip_of_the_release_branch(rc1):
    rc1.commit("a newer commit on the release branch")
    rc1.git("push", "-q", "origin", BRANCH)
    before = refs_snapshot(rc1)
    r = replace(rc1)  # NEW is on the branch but no longer its tip
    assert_untouched(rc1, r, before, "must be the tip of origin/" + BRANCH)


def test_a_local_copy_of_the_tag_that_differs_from_origins_stops(rc1):
    rc1.git("tag", "-a", "-m", "local only, annotated", RC1, rc1.old_sha)
    before = refs_snapshot(rc1)
    assert_untouched(rc1, replace(rc1), before, "inconsistent")


def test_the_same_local_lightweight_copy_is_fine(rc1):
    rc1.git("tag", RC1, rc1.old_sha)  # what the workflow's checkout (fetch-tags) has
    assert replace(rc1).returncode == 0


def test_a_missing_tag_takes_the_normal_annotated_creation_path(rc1):
    rc1.git("push", "-q", "origin", f":refs/tags/{RC1}")  # test setup only: no tag on origin
    r = replace(rc1)
    assert r.returncode == 0, r.stderr
    assert "mode: create" in r.stdout
    calls = gh_calls(rc1)
    assert len(calls) == 3 and not any("DELETE" in c for c in calls)
    assert rc1.git("ls-remote", "origin", f"refs/tags/{RC1}^{{}}").split()[0] == rc1.new_sha


def test_a_failure_creating_the_tag_object_leaves_the_old_tag_untouched(rc1):
    r = replace(rc1, extra_env={"SHIM_FAIL_TAG_OBJECT": "1"})
    assert r.returncode == 1 and "no tag was touched" in r.stderr
    assert len(gh_calls(rc1)) == 1  # nothing after the failed object: no delete, no ref
    assert remote_rc1(rc1).split() == [rc1.old_sha, f"refs/tags/{RC1}"]


@pytest.mark.parametrize("shim", [{"SHIM_FAIL_READBACK": "1"}, {"SHIM_WRONG_PEEL": "x"}])
def test_a_tag_object_that_cannot_be_read_back_correctly_stops_before_the_delete(rc1, shim):
    shim = {k: (rc1.release_sha if v == "x" else v) for k, v in shim.items()}
    r = replace(rc1, extra_env=shim)
    assert r.returncode == 1 and "no tag was touched" in r.stderr
    calls = gh_calls(rc1)
    assert len(calls) == 2 and not any("DELETE" in c for c in calls)
    assert remote_rc1(rc1).split() == [rc1.old_sha, f"refs/tags/{RC1}"]


def test_a_tag_moved_while_the_script_ran_is_not_deleted(rc1):
    r = replace(rc1, extra_env={"SHIM_MOVE_TAG": "1", "SHIM_MOVE_TO": rc1.release_sha})
    assert r.returncode == 1 and "changed while the script ran" in r.stderr
    assert not any("DELETE" in c for c in gh_calls(rc1))
    assert remote_rc1(rc1).split()[0] == rc1.release_sha  # whoever moved it, it stands


def test_a_failed_delete_leaves_the_old_tag_and_reports_its_state(rc1):
    r = replace(rc1, extra_env={"SHIM_FAIL_DELETE": "1"})
    assert r.returncode == 1 and "DELETE of the old lightweight" in r.stderr
    assert rc1.old_sha in r.stderr  # the state is printed
    assert len(gh_calls(rc1)) == 3  # object, read-back, the failed delete: and nothing else
    assert remote_rc1(rc1).split() == [rc1.old_sha, f"refs/tags/{RC1}"]


def test_a_failure_after_the_delete_reports_that_the_tag_is_temporarily_absent(rc1):
    r = replace(rc1, extra_env={"SHIM_FAIL_REF": "1"})
    assert r.returncode == 1
    assert "TEMPORARILY ABSENT" in r.stderr and "was deleted" in r.stderr
    assert f"-f sha={rc1.old_sha}" in r.stderr  # how to restore the old lightweight tag
    tag_object = re.search(r"-f sha=([0-9a-f]{40})\n", r.stderr).group(1)
    assert tag_object not in (rc1.old_sha, rc1.new_sha)  # the annotated object made earlier
    assert f"-f ref=refs/tags/{RC1} -f sha={tag_object}" in r.stderr  # how to put it in place
    assert "Nothing was retried" in r.stderr
    assert remote_rc1(rc1) == ""  # the tag really is absent now
    assert rc1.git("--git-dir", str(rc1.remote), "cat-file", "-t", tag_object) == "tag"
    calls = gh_calls(rc1)
    assert len(calls) == 4 and "DELETE" in calls[2] and "POST" in calls[3]  # a single attempt


def test_the_final_read_back_catches_a_lightweight_ref_and_says_the_old_tag_is_gone(rc1):
    r = replace(rc1, extra_env={"SHIM_LIGHTWEIGHT": "1"})
    assert r.returncode == 1 and "VERIFY FAILED" in r.stderr
    assert (
        "lightweight" in r.stderr
        and f"The old lightweight tag ({rc1.old_sha}) was deleted" in r.stderr
    )
    assert len(gh_calls(rc1)) == 4  # no repair attempt


def test_a_second_run_after_a_successful_replacement_touches_nothing(rc1):
    assert replace(rc1).returncode == 0
    first = remote_rc1(rc1)
    before = refs_snapshot(rc1)
    calls = len(gh_calls(rc1))
    r = replace(rc1)
    assert r.returncode == 1 and "ANNOTATED" in r.stderr
    assert remote_rc1(rc1) == first and refs_snapshot(rc1) == before and len(gh_calls(rc1)) == calls


def test_replacement_never_changes_the_release_branch(rc1):
    tip = rc1.git("ls-remote", "origin", f"refs/heads/{BRANCH}")
    assert replace(rc1).returncode == 0
    assert rc1.git("ls-remote", "origin", f"refs/heads/{BRANCH}") == tip


# ------------------------------------------------------------------ the script's code


def script_code():
    return "\n".join(x for x in SCRIPT.read_text().splitlines() if not x.lstrip().startswith("#"))


def test_the_script_has_no_way_to_move_force_update_or_overwrite_a_tag():
    code = script_code()
    for forbidden in (
        "git push", "git tag", "update-ref", "--force", "force-with-lease", "--delete", "tag -d",
        "-X PATCH", "-X PUT", "-X DELETE", "--method PATCH", "--method PUT", "git reset",
        "git checkout", "rm -", "branch -D",
    ):  # fmt: skip
        assert forbidden not in code, forbidden
    # the only API calls, in source order: tag object, ref (create), DELETE, ref (replace)
    calls = re.findall(r'--method (\w+) "repos/\$GITHUB_REPOSITORY/git/([a-z/$A-Z_]+)', code)
    assert calls == [
        ("POST", "tags"),
        ("POST", "refs"),
        ("DELETE", "refs/tags/$TAG"),
        ("POST", "refs"),
    ]
    assert code.count("--method DELETE") == 1


def test_the_one_delete_comes_after_the_tag_object_and_the_ref_is_created_right_after_it():
    code = script_code()
    replace = code.split("replace_mode() {")[1].split("\n}\n")[0]
    assert (
        replace.index("gh_post_tag_object")
        < replace.index("changed while the script ran")
        < replace.index("--method DELETE")
        < replace.index("--method POST")
    )
    after_delete = replace.split("--method DELETE")[1]
    assert (
        after_delete.count("gh api") == 1 + 2
    )  # the POST, plus the two recovery commands in the text
    assert "TEMPORARILY ABSENT" in after_delete and "Nothing was retried" in after_delete


def test_the_script_validates_before_it_creates_and_creates_the_object_before_the_ref():
    code = script_code()
    assert code.index("validate() {") < code.index("gh_post_tag_object() {")
    assert code.index("gh_post_tag_object() {") < code.index("create_mode() {")
    create_mode = code.split("create_mode() {")[1].split("\n}\n")[0]
    assert create_mode.index("gh_post_tag_object") < create_mode.index("git/refs")
    main = code.split("\nvalidate\ncase")[1]
    assert main.index("DRY_RUN") < main.index("create_mode") < main.index("replace_mode")
    assert code.index("\nvalidate\ncase") < code.index(
        "((DRY_RUN))"
    )  # validated before the dry-run exit


def test_the_allowed_replacement_is_hard_coded_to_exactly_the_known_case():
    text = SCRIPT.read_text()
    assert 'REPLACE_ALLOW_TAG="${REPLACE_ALLOW_TAG:-v2.0.0-rc1}"' in text
    assert "REPLACE_ALLOW_OLD_SHA:-74214af5aa2be3a71dc883d5a36b9c6296cc0f37}" in text
    assert "REPLACE_ALLOW_NEW_SHA:-69566e3b8ef8b4c64c42c5aa6d69c9421bb12ca7}" in text


# ------------------------------------------------------------------ the workflow


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_the_workflow_is_manual_only_with_the_required_inputs(workflow):
    on = workflow.get("on", workflow.get(True))
    assert list(on) == ["workflow_dispatch"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"tag", "expected_sha", "dry_run", "replace_lightweight"}
    assert inputs["tag"]["required"] is True and inputs["expected_sha"]["required"] is True
    assert inputs["dry_run"]["default"] is True  # the safe choice unless somebody turns it off
    assert inputs["replace_lightweight"]["default"] is False  # replacing is always a request


def test_the_workflow_asks_for_no_permission_except_contents_write_on_its_one_job(workflow):
    assert workflow["permissions"] == {}
    assert list(workflow["jobs"]) == ["create-tag"]
    job = workflow["jobs"]["create-tag"]
    assert job["permissions"] == {"contents": "write"}


def test_the_workflow_runs_only_from_the_release_branch_and_checks_it_out_in_full(workflow):
    job = workflow["jobs"]["create-tag"]
    assert job["if"] == "github.ref == 'refs/heads/release/v2.0.0'"
    checkout = job["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["fetch-depth"] == 0 and checkout["with"]["ref"] == "release/v2.0.0"


def test_the_workflow_passes_the_inputs_through_the_environment_not_the_script_text(workflow):
    step = workflow["jobs"]["create-tag"]["steps"][1]
    assert "${{" not in step["run"]  # no expression is ever expanded into shell text
    assert step["env"]["TAG"] == "${{ inputs.tag }}"
    assert step["env"]["EXPECTED_SHA"] == "${{ inputs.expected_sha }}"
    assert "scripts/create_release_tag.sh" in step["run"] and "--dry-run" in step["run"]
    assert "--replace-lightweight" in step["run"]
    assert step["env"]["REPLACE_LIGHTWEIGHT"] == "${{ inputs.replace_lightweight }}"
    assert not any(
        k.startswith("REPLACE_ALLOW") for k in step["env"]
    )  # the allow-list is the script's
    assert step["env"]["RELEASE_BRANCH"] == "release/v2.0.0"


def test_the_workflow_itself_has_no_git_or_api_write_commands():
    text = "\n".join(x for x in WORKFLOW.read_text().splitlines() if not x.lstrip().startswith("#"))
    for forbidden in (
        "git push",
        "git tag",
        "--force",
        "--delete",
        "update-ref",
        "DELETE",
        "PATCH",
    ):
        assert forbidden not in text, forbidden
    assert "pull_request" not in text and "push:" not in text and "schedule" not in text


def test_the_workflow_is_the_only_one_and_the_script_it_calls_exists():
    workflows = sorted(p.name for p in (ROOT / ".github" / "workflows").iterdir())
    assert workflows == ["create-release-tag.yml"]
    assert os.access(SCRIPT, os.R_OK)
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0  # noqa: S603,S607
