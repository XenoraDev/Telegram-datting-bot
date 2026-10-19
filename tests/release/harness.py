"""A throw-away git repository + a fake `docker` for exercising scripts/release.sh.

Nothing real is touched: the repository lives in a temp directory (with a local bare repo as its
"origin"), `docker` is a python script that records every call and answers from state files, and
the backup script is a stub. The fake REFUSES (exit 99, logged as FORBIDDEN) anything destructive:
down, rm, volume, prune, system, rmi, kill.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET = "SUPERSECRETVALUE-do-not-print-123"
BRANCH = "claude/dating-bot-v2"

DOCKER = r"""#!/usr/bin/env python3
import os, sys, pathlib

S = pathlib.Path(os.environ["SHIM_STATE"])
args = sys.argv[1:]
with open(S / "calls.log", "a") as f:
    f.write("docker " + " ".join(args) + "\n")


def st(name, default=""):
    p = S / name
    return p.read_text().strip() if p.exists() else default


def put(name, value):
    (S / name).write_text(str(value))


FORBIDDEN = {"down", "rm", "volume", "prune", "system", "rmi", "kill", "network"}
if FORBIDDEN & set(args) or any(a in ("-v", "--volumes", "--remove-orphans") for a in args):
    with open(S / "calls.log", "a") as f:
        f.write("FORBIDDEN\n")
    print("forbidden in this test", file=sys.stderr)
    sys.exit(99)

tag_sha = lambda tag: st("tag_sha_" + tag, "unknown")


def health(svc):
    tag = st(f"run_{svc}_tag")
    if not tag:
        return "missing/none"
    if st("unhealthy_tags") and tag in st("unhealthy_tags").split():
        return "running/unhealthy"
    return "running/healthy"


if args[:1] == ["compose"]:
    rest = args[1:]
    while rest[:1] == ["--profile"]:
        rest = rest[2:]
    sub = rest[0] if rest else ""
    tag = os.environ.get("RELEASE_TAG", "dev")
    if sub in ("build", "run", "up"):
        with open(S / "calls.log", "a") as f:
            f.write(
                "ENV %s RELEASE_TAG=%s GIT_SHA=%s ALLOW_NEWER_SCHEMA=%s\n"
                % (
                    sub,
                    tag,
                    os.environ.get("GIT_SHA", ""),
                    os.environ.get("ALLOW_NEWER_SCHEMA", ""),
                )
            )
    if sub == "version":
        sys.exit(0)
    if sub == "config":
        sys.exit(1 if st("bad_config") else 0)
    if sub == "ps":
        svc = rest[-1]
        if svc == "db":
            print("db_c1")
        elif st(f"run_{svc}_tag"):
            print(f"{svc}_c1")
        sys.exit(0)
    if sub == "build":
        if os.environ.get("SHIM_FAIL_BUILD") or st("fail_build"):
            print("build failed", file=sys.stderr)
            sys.exit(1)
        if st("build_sleep"):
            import time

            time.sleep(int(st("build_sleep")))
        put("tag_sha_" + tag, os.environ.get("GIT_SHA", "unknown"))
        with open(S / "images", "a") as f:
            f.write(f"bot:{tag}\nweb:{tag}\n")
        sys.exit(0)
    if sub == "run":
        if "--status" in rest:
            print("latest_applied=\npending=\nok=1")
        elif "--plan" in rest:
            print("pending=\n" + st("plan_lines_" + tag, "rollback_safe=1"))
        elif "--check-rollback" in rest:
            if st("refuse_rollback"):
                print("allowed=0\nblocked=008 is a contract migration: shim")
                sys.exit(3)
            print("allowed=1")
            if st("newer"):
                print("newer=" + st("newer"))
        else:  # apply migrations
            if st("fail_migrate"):
                print("migration error: shim", file=sys.stderr)
                sys.exit(1)
            put("db_latest", st("schema_" + tag, "21"))
            print("applied")
        sys.exit(0)
    if sub == "up":
        if st("fail_up"):
            sys.exit(1)
        svc = rest[-1]
        put(f"run_{svc}_tag", tag)
        put(f"run_{svc}_sha", tag_sha(tag))
        sys.exit(0)
    sys.exit(0)

if args[:2] == ["image", "inspect"]:
    img = args[2]  # dating-bot/<svc>:<tag>
    svc, _, tag = img.split("/")[1].partition(":")
    known = (S / "images").read_text().split() if (S / "images").exists() else []
    sys.exit(0 if f"{svc}:{tag}" in known else 1)

if args[:1] == ["inspect"]:
    fmt, cid = args[2], args[3]
    svc = cid.split("_")[0]
    if cid.startswith("dating-bot/"):  # an image, not a container
        if "image.revision" in fmt:
            print(tag_sha(cid.split(":", 1)[1]))
        sys.exit(0)
    if "image.revision" in fmt:
        print(st(f"run_{svc}_sha"))
    elif "image.version" in fmt:
        print(st(f"run_{svc}_tag"))
    elif "State.Status" in fmt:
        if svc == "db":
            print("running/unhealthy" if st("db_unhealthy") else "running/healthy")
        else:
            print(health(svc))
    sys.exit(0)

if args[:1] == ["run"]:  # docker run --rm --entrypoint python <image> -m bot.db.migrate --info
    img = next(a for a in args if a.startswith("dating-bot/"))
    tag = img.split(":", 1)[1]
    print("latest_migration=" + st("schema_" + tag, "21"))
    print("schema_tolerant=" + st("tolerant_" + tag, "1"))
    sys.exit(0)

if args[:1] == ["exec"]:
    if st("db_latest"):
        print("%03d_something" % int(st("db_latest")))
    sys.exit(0)

if args[:1] == ["logs"]:
    cid = args[-1]
    if st("no_polling"):
        print("nothing useful")
    elif st("conflict"):
        print("starting long polling")
        print("TelegramConflictError: Conflict: terminated by other getUpdates")
    else:
        print("starting long polling")
    sys.exit(0)

sys.exit(0)
"""

CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$SHIM_STATE/calls.log"
[[ -f "$SHIM_STATE/web_down" ]] && exit 22
echo ok
"""

BACKUP = r"""#!/usr/bin/env bash
echo "BACKUP-SCRIPT $*" >> "$SHIM_STATE/calls.log"
[[ -f "$SHIM_STATE/fail_backup" ]] && { echo "backup failed" >&2; exit 1; }
echo "backup written: ./backups/dating_20260101T000000Z.sql.gz (1K)"
"""


def run(cmd, cwd, env=None, check=True, **kw):
    return subprocess.run(  # noqa: S603
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=check, **kw
    )


@dataclass
class Rig:
    tmp: Path
    repo: Path
    remote: Path
    state: Path
    bin: Path
    env: dict
    shas: dict = field(default_factory=dict)

    # ---- git
    def git(self, *a, check=True):
        return run(["git", *a], self.repo, self.env, check=check).stdout.strip()

    def commit(self, name="change", content=None):
        (self.repo / f"{name}.txt").write_text(content or name)
        self.git("add", ".")
        self.git("commit", "-q", "-m", name)
        return self.git("rev-parse", "HEAD")

    def tag(self, tag, sha=None, annotated=True, push=True):
        sha = sha or self.git("rev-parse", "HEAD")
        if annotated:
            self.git("tag", "-a", "-m", f"release {tag}", tag, sha)
        else:
            self.git("tag", tag, sha)
        if push:
            self.git("push", "-q", "origin", BRANCH, "--tags")
        self.shas[tag] = sha
        return sha

    # ---- fake docker state
    def put(self, name, value):
        (self.state / name).write_text(str(value))

    def get(self, name, default=""):
        p = self.state / name
        return p.read_text().strip() if p.exists() else default

    def running(self, tag, sha=None, schema=21):
        """Pretend this release is what the bot and web containers are running now."""
        sha = sha or self.shas[tag]
        for svc in ("bot", "web"):
            self.put(f"run_{svc}_tag", tag)
            self.put(f"run_{svc}_sha", sha)
        with open(self.state / "images", "a") as f:
            f.write(f"bot:{tag}\nweb:{tag}\n")
        self.put("tag_sha_" + tag, sha)
        self.put("schema_" + tag, schema)
        self.put("db_latest", schema)

    def calls(self):
        p = self.state / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def compose_calls(self, sub):
        return [c for c in self.calls() if c.startswith("docker compose") and f" {sub} " in c + " "]

    def ledger(self):
        p = self.repo / "releases" / "history.jsonl"
        return p.read_text().splitlines() if p.exists() else []

    # ---- the script
    def release(self, *args, check=False, extra_env=None, input=None):
        env = {**self.env, **(extra_env or {})}
        return subprocess.run(  # noqa: S603
            ["bash", str(self.repo / "scripts" / "release.sh"), *args],  # noqa: S607
            cwd=self.repo, env=env, capture_output=True, text=True, check=check,
            input=input, timeout=120,
        )  # fmt: skip


def build_rig(tmp: Path) -> Rig:
    remote = tmp / "remote.git"
    repo = tmp / "repo"
    state = tmp / "state"
    bin_dir = tmp / "bin"
    for d in (state, bin_dir):
        d.mkdir()
    run(["git", "init", "-q", "--bare", str(remote)], tmp)
    run(["git", "clone", "-q", str(remote), str(repo)], tmp)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp),
        "SHIM_STATE": str(state),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        "HEALTH_TIMEOUT": "4", "HEALTH_INTERVAL": "1", "MIN_FREE_MB": "1",
        "BACKUP_SCRIPT": str(bin_dir / "backup-stub"),
        "SUDO_USER": "operator1",
        "TMPDIR": str(tmp),
    }  # fmt: skip
    (bin_dir / "docker").write_text(DOCKER)
    (bin_dir / "curl").write_text(CURL)
    (bin_dir / "backup-stub").write_text(BACKUP)
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    rig = Rig(tmp, repo, remote, state, bin_dir, env)
    rig.git("checkout", "-q", "-b", BRANCH)
    (repo / "scripts").mkdir()
    for rel in ("scripts/release.sh", "scripts/_common.sh", "scripts/backup.sh", "Dockerfile",
                "docker-compose.yml", "release/migration_compat.json"):  # fmt: skip
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, dest)
    (repo / ".gitignore").write_text("releases/\n.env\n")
    (repo / ".env").write_text(
        "\n".join(f"{k}=value-of-{k}" for k in (
            "BOT_TOKEN", "ADMIN_IDS", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB",
            "ADMIN_DOMAIN", "ADMIN_SESSION_SECRET", "ADMIN_BOT_USERNAME")) + f"\nEXTRA={SECRET}\n"
    )  # fmt: skip
    rig.git("add", ".")
    rig.git("commit", "-q", "-m", "base")
    rig.git("push", "-q", "-u", "origin", BRANCH)
    return rig
