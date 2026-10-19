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
import os, subprocess, sys

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


method = args[args.index("--method") + 1] if "--method" in args else "GET"
if method != "POST" or "force" in " ".join(args).lower():
    with open(LOG, "a") as f:
        f.write("FORBIDDEN\n")
    sys.exit(99)
endpoint = next(a for a in args if a.startswith("repos/"))
fields = {}
for i, a in enumerate(args):
    if a == "-f":
        k, _, v = args[i + 1].partition("=")
        fields[k] = v

if endpoint.endswith("/git/tags"):
    if mode("FAIL_TAG_OBJECT"):
        print("HTTP 422", file=sys.stderr)
        sys.exit(1)
    target = os.environ["SHIM_WRONG_PEEL"] if mode("WRONG_PEEL") else fields["object"]
    body = (
        f"object {target}\ntype commit\ntag {fields['tag']}\n"
        f"tagger bot <bot@example.com> 0 +0000\n\n{fields['message']}\n"
    )
    print(git("mktag", input=body).stdout.strip())
    sys.exit(0)

if endpoint.endswith("/git/refs"):
    ref, sha = fields["ref"], fields["sha"]
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


def assert_refused_before_any_write(rig, r, before, text=None):
    assert r.returncode == 1, r.stdout + r.stderr
    assert "NOTHING WAS WRITTEN" in r.stderr
    if text:
        assert text in r.stderr, r.stderr
    assert gh_calls(rig) == []  # not a single API call
    assert refs_snapshot(rig) == before


# ------------------------------------------------------------------ the happy path


def test_creates_an_annotated_tag_with_the_tag_object_first_then_the_ref(rig):
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
    assert len(calls) == 2 and "FORBIDDEN" not in calls
    assert "--method POST repos/XenoraDev/Telegram-datting-bot/git/tags" in calls[0]
    assert "--method POST repos/XenoraDev/Telegram-datting-bot/git/refs" in calls[1]
    assert f"-f ref=refs/tags/{TAG}" in calls[1] and f"-f sha={tag_object}" in calls[1]
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
    assert "validate-only: nothing was written" in r.stdout
    assert gh_calls(rig) == [] and refs_snapshot(rig) == before


# ------------------------------------------------------------------ refused before any write


@pytest.mark.parametrize("annotated", [True, False])
def test_an_existing_tag_on_the_remote_is_refused_and_left_untouched(rig, annotated):
    rig.tag(TAG, rig.release_sha, annotated=annotated)  # pushed to origin
    rig.git("tag", "-d", TAG)  # the checkout does not have it: the REMOTE check must catch it
    before = refs_snapshot(rig)
    assert_refused_before_any_write(rig, create(rig), before, "already exists on origin")


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
    assert_refused_before_any_write(rig, r, before, "never overwritten, moved or deleted")
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
        len(calls) == 2 and "FORBIDDEN" not in calls
    )  # no repair attempt: no delete, no re-create
    assert rig.git("ls-remote", "origin", f"refs/tags/{TAG}").split()[0] == rig.release_sha


def test_verification_catches_a_tag_that_peels_to_another_commit(rig):
    other = rig.commit("other")
    rig.git("push", "-q", "origin", BRANCH)
    r = create(rig, extra_env={"SHIM_WRONG_PEEL": other})
    assert r.returncode == 1 and "VERIFY FAILED" in r.stderr and "peels to" in r.stderr
    assert len(gh_calls(rig)) == 2 and "FORBIDDEN" not in gh_calls(rig)
    assert "Do not re-tag" in r.stderr
    assert rig.git("ls-remote", "origin", f"refs/tags/{TAG}^{{}}").split()[0] == other  # left as is


def test_a_tag_created_by_somebody_else_in_the_meantime_is_not_overwritten(rig):
    rival = rig.commit("rival")
    rig.git("push", "-q", "origin", BRANCH)
    r = create(rig, extra_env={"SHIM_RACE": rival})
    assert r.returncode == 1 and "Nothing was overwritten" in r.stderr
    assert rig.git("ls-remote", "origin", f"refs/tags/{TAG}").split()[0] == rival  # theirs stands
    calls = gh_calls(rig)
    assert len(calls) == 2 and "FORBIDDEN" not in calls


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
    assert len(gh_calls(rig)) == 2  # the second run made no call at all


def test_the_fake_gh_rejects_anything_but_a_post(rig):
    gh = rig.bin / "gh"
    env = {**rig.env, "FAKE_REMOTE": str(rig.remote)}
    for method in ("PATCH", "DELETE", "PUT"):
        done = subprocess.run(  # noqa: S603
            [str(gh), "api", "--method", method, "repos/o/r/git/refs/tags/x"],
            env=env, capture_output=True, text=True, check=False,
        )  # fmt: skip
        assert done.returncode == 99
    assert gh_calls(rig).count("FORBIDDEN") == 3


# ------------------------------------------------------------------ the script's code


def script_code():
    return "\n".join(x for x in SCRIPT.read_text().splitlines() if not x.lstrip().startswith("#"))


def test_the_script_has_no_way_to_delete_move_or_overwrite_a_tag():
    code = script_code()
    for forbidden in (
        "git push", "git tag", "update-ref", "--force", "force-with-lease", "--delete",
        "tag -d", "-X PATCH", "-X PUT", "-X DELETE", "--method PATCH", "--method PUT",
        "--method DELETE", "git reset", "git checkout", "rm -", "branch -D",
    ):  # fmt: skip
        assert forbidden not in code, forbidden
    assert code.count("--method POST") == 2
    assert re.findall(r"--method (\w+)", code) == ["POST", "POST"]


def test_the_script_validates_before_it_creates_and_creates_the_object_before_the_ref():
    code = script_code()
    main = code.split("\nvalidate\nsay")[1]
    assert main.index("VALIDATE_ONLY") < main.index("\ncreate\n") < main.index("\nverify\n")
    body = code.split("create() {")[1]
    assert body.index("git/tags") < body.index("git/refs")
    assert code.index("validate() {") < code.index("create() {") < code.index("verify() {")


# ------------------------------------------------------------------ the workflow


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_the_workflow_is_manual_only_with_the_required_inputs(workflow):
    on = workflow.get("on", workflow.get(True))
    assert list(on) == ["workflow_dispatch"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"tag", "expected_sha", "dry_run"}
    assert inputs["tag"]["required"] is True and inputs["expected_sha"]["required"] is True
    assert inputs["dry_run"]["default"] is True  # the safe choice unless somebody turns it off


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
    assert "scripts/create_release_tag.sh" in step["run"] and "--validate-only" in step["run"]
    assert step["env"]["RELEASE_BRANCH"] == "release/v2.0.0"


def test_the_workflow_cannot_delete_move_or_overwrite_a_tag():
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
