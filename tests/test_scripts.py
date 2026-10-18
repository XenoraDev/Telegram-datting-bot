"""backup.sh / restore.sh against a fake `docker` that REJECTS `docker compose exec`.

Regression for the aaPanel VPS failure:  unknown shorthand flag: 'T' in -T
The scripts must work without ever calling `compose exec`; pg_dump / psql go through plain
`docker exec`. The fake docker records every call so the exact command lines can be asserted.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DUMP = "-- PostgreSQL database dump\nCREATE TABLE secret_marker (id int);\n"

DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$SHIM_LOG"
reject() { echo "unknown shorthand flag: 'T' in -T" >&2; exit 125; }
if [[ "$1" == compose ]]; then
    shift
    case "$1" in
        version) [[ -z "${SHIM_NO_PLUGIN:-}" ]] && exit 0 || exit 1 ;;
        exec) reject ;;                               # <- what the VPS does
        ps) [[ -z "${SHIM_NO_DB:-}" ]] && echo "cid123"; exit 0 ;;
        stop|start) exit 0 ;;
    esac
    exit 1
fi
if [[ "$1" == exec ]]; then
    shift
    while [[ "$1" == -* ]]; do shift; done            # exec flags
    shift                                             # container
    case "$1" in
        pg_dump)
            if [[ -n "${SHIM_DUMP_FILE:-}" ]]; then cat "$SHIM_DUMP_FILE"
            elif [[ -z "${SHIM_EMPTY_DUMP:-}" ]]; then printf '%s' "$SHIM_DUMP"; fi
            exit 0 ;;
        psql) cat > "$SHIM_PSQL_IN"; exit "${SHIM_PSQL_EXIT:-0}" ;;
    esac
fi
[[ "$1" == stop || "$1" == start ]] && exit 0
exit 1
"""

LEGACY = r"""#!/usr/bin/env bash
echo "docker-compose $*" >> "$SHIM_LOG"
[[ "$1" == ps ]] && { echo "legacy456"; exit 0; }
[[ "$1" == exec ]] && { echo "unknown shorthand flag: 'T' in -T" >&2; exit 125; }
exit 0
"""


# A `gzip` in front of the real one that makes the COMPRESSING call (gzip -9) write a truncated
# file: what a disk-full / killed-mid-write backup looks like. Every other call is the real gzip.
GZIP = r"""#!/usr/bin/env bash
if [[ -n "${SHIM_CORRUPT_GZIP:-}" && "$1" == -9 ]]; then
    "$SHIM_REAL_GZIP" "$@" | head -c 40
    exit 0
fi
exec "$SHIM_REAL_GZIP" "$@"
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(DOCKER)
    (bin_dir / "docker").chmod(0o755)
    (bin_dir / "gzip").write_text(GZIP)
    (bin_dir / "gzip").chmod(0o755)
    e = {k: v for k, v in os.environ.items() if not k.startswith(("POSTGRES_", "BACKUP_", "SHIM_"))}
    e.update(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        SHIM_REAL_GZIP=shutil.which("gzip"),
        SHIM_LOG=str(tmp_path / "calls.log"),
        SHIM_DUMP=DUMP,
        SHIM_PSQL_IN=str(tmp_path / "psql.in"),
        POSTGRES_USER="dating_bot",
        POSTGRES_DB="dating_bot",
        BACKUP_DIR=str(tmp_path / "backups"),
        BACKUP_KEEP_DAYS="7",
    )
    e["_bin"] = str(bin_dir)  # helper for tests, stripped before use
    return e


def run(script: str, env: dict, *args: str):
    e = {k: v for k, v in env.items() if k != "_bin"}
    return subprocess.run(  # noqa: S603 - fixed repo script + test-controlled args
        ["bash", str(ROOT / "scripts" / script), *args],  # noqa: S607
        env=e,
        capture_output=True,
        text=True,
    )


def calls(env) -> list[str]:
    p = Path(env["SHIM_LOG"])
    return p.read_text().splitlines() if p.exists() else []


def backups(env) -> list[Path]:
    d = Path(env["BACKUP_DIR"])
    return sorted(d.glob("*")) if d.exists() else []


pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")


# ---------------- backup.sh ----------------


def test_backup_works_even_though_compose_exec_is_rejected(env):
    r = run("backup.sh", env)
    assert r.returncode == 0, r.stderr
    (f,) = backups(env)
    assert f.name.startswith("dating_bot_") and f.name.endswith(".sql.gz")
    assert "secret_marker" in gzip.decompress(f.read_bytes()).decode()
    assert not [c for c in calls(env) if c.startswith("docker compose exec")]


def test_backup_uses_plain_docker_exec_without_tty_or_dash_T(env):
    run("backup.sh", env)
    dump_calls = [c for c in calls(env) if " pg_dump " in c]
    assert len(dump_calls) == 1
    line = dump_calls[0]
    assert line.startswith("docker exec cid123 pg_dump -U dating_bot -d dating_bot ")
    assert "--clean" in line and "--if-exists" in line
    tokens = line.split()
    assert "-T" not in tokens and "-t" not in tokens and "-it" not in tokens  # non-interactive


def test_backup_script_never_mentions_compose_exec(env):
    for name in ("backup.sh", "restore.sh", "_common.sh"):
        text = (ROOT / "scripts" / name).read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert "compose exec" not in code and "exec -T" not in code, name


def test_empty_dump_is_rejected_and_leaves_nothing_behind(env):
    env["SHIM_EMPTY_DUMP"] = "1"
    r = run("backup.sh", env)
    assert r.returncode == 1 and "empty/invalid" in r.stderr
    assert backups(env) == []


LARGE_MB = 24  # far more than a pipe buffer (64 KB): the reader of `gzip -dc` stops long before EOF


def big_dump(path: Path, *, marker: str = "head", mb: int = LARGE_MB) -> Path:
    """A large, valid-looking pg_dump text. The PostgreSQL marker is on the FIRST line (the
    early-exit case), on the LAST line, or absent."""
    row = "INSERT INTO public.messages VALUES (1, 'lorem ipsum dolor sit amet consectetur');\n"
    body = row * (mb * 1024 * 1024 // len(row))
    header = "-- PostgreSQL database dump\n\n"
    text = {"head": header + body, "tail": body + header, "none": body}[marker]
    path.write_text(text)
    return path


def test_the_old_check_really_fails_on_a_large_dump_under_pipefail(tmp_path):
    """The bug itself, in isolation: `grep -q` stops at the first match, gzip gets SIGPIPE, and
    pipefail turns that into a failed pipeline although the dump is perfectly valid."""
    dump = big_dump(tmp_path / "d.sql")
    gz = tmp_path / "d.sql.gz"
    script = (
        'set -o pipefail; gzip -9 -c "$1" > "$2"; '
        'gzip -dc "$2" | grep -q "PostgreSQL database dump"'
    )
    old = subprocess.run(  # noqa: S603 - fixed command, test-made paths
        ["bash", "-c", script, "x", str(dump), str(gz)],  # noqa: S607
        capture_output=True,
    )
    assert old.returncode != 0  # false failure on a valid file (141 = killed by SIGPIPE)


@pytest.mark.parametrize("marker", ["head", "tail"])
def test_a_large_valid_dump_is_accepted(env, tmp_path, marker):
    env["SHIM_DUMP_FILE"] = str(big_dump(tmp_path / "big.sql", marker=marker))
    r = run("backup.sh", env)
    assert r.returncode == 0, r.stderr
    (f,) = backups(env)
    assert f.name.endswith(".sql.gz") and f.stat().st_mode & 0o777 == 0o600
    with gzip.open(f, "rt") as fh:
        assert "PostgreSQL database dump" in fh.readline() or marker == "tail"


def test_a_large_dump_that_is_not_postgresql_is_rejected(env, tmp_path):
    env["SHIM_DUMP_FILE"] = str(big_dump(tmp_path / "big.sql", marker="none"))
    r = run("backup.sh", env)
    assert r.returncode == 1 and "empty/invalid" in r.stderr
    assert backups(env) == []


def test_a_corrupt_gzip_is_rejected_and_leaves_nothing_behind(env):
    env["SHIM_CORRUPT_GZIP"] = "1"
    r = run("backup.sh", env)
    assert r.returncode != 0 and backups(env) == []


def test_a_corrupt_gzip_of_a_large_dump_is_rejected_too(env, tmp_path):
    env["SHIM_CORRUPT_GZIP"] = "1"
    env["SHIM_DUMP_FILE"] = str(big_dump(tmp_path / "big.sql"))
    r = run("backup.sh", env)
    assert r.returncode != 0 and backups(env) == []


def test_non_gzip_or_garbage_dump_is_rejected(env):
    env["SHIM_DUMP"] = "this is not a pg_dump"
    r = run("backup.sh", env)
    assert r.returncode == 1 and backups(env) == []


def test_rotation_deletes_only_old_backups_of_this_database(env, tmp_path):
    d = Path(env["BACKUP_DIR"])
    d.mkdir()
    old, fresh, other_db, note = (
        d / "dating_bot_old.sql.gz",
        d / "dating_bot_recent.sql.gz",
        d / "otherdb_old.sql.gz",
        d / "notes.txt",
    )
    for f in (old, fresh, other_db, note):
        f.write_text("x")
    long_ago = time.time() - 30 * 86400
    for f in (old, other_db, note):
        os.utime(f, (long_ago, long_ago))
    assert run("backup.sh", env).returncode == 0
    names = {p.name for p in backups(env)}
    assert "dating_bot_old.sql.gz" not in names
    assert {"dating_bot_recent.sql.gz", "otherdb_old.sql.gz", "notes.txt"} <= names


def test_backup_file_permissions_are_private(env):
    run("backup.sh", env)
    (f,) = backups(env)
    assert f.stat().st_mode & 0o777 == 0o600
    assert Path(env["BACKUP_DIR"]).stat().st_mode & 0o777 == 0o700


def test_invalid_retention_value_is_refused(env):
    env["BACKUP_KEEP_DAYS"] = "abc"
    r = run("backup.sh", env)
    assert r.returncode == 2 and backups(env) == []


def test_db_container_override_skips_compose_entirely(env):
    env["SHIM_NO_PLUGIN"] = "1"  # no compose of any kind on this host
    env["DB_CONTAINER"] = "my-postgres"
    r = run("backup.sh", env)
    assert r.returncode == 0, r.stderr
    assert any(c.startswith("docker exec my-postgres pg_dump") for c in calls(env))
    assert not [c for c in calls(env) if "compose" in c and "version" not in c]


def test_falls_back_to_legacy_docker_compose_binary(env):
    legacy = Path(env["_bin"]) / "docker-compose"
    legacy.write_text(LEGACY)
    legacy.chmod(0o755)
    env["SHIM_NO_PLUGIN"] = "1"
    r = run("backup.sh", env)
    assert r.returncode == 0, r.stderr
    assert any(c.startswith("docker exec legacy456 pg_dump") for c in calls(env))
    assert not [c for c in calls(env) if c.startswith("docker-compose exec")]


def test_clear_error_when_the_db_container_is_not_running(env):
    env["SHIM_NO_DB"] = "1"
    r = run("backup.sh", env)
    assert r.returncode != 0 and "not running" in r.stderr
    assert backups(env) == []


def test_clear_error_when_no_compose_and_no_override(env):
    env["SHIM_NO_PLUGIN"] = "1"
    r = run("backup.sh", env)
    assert r.returncode != 0 and "DB_CONTAINER" in r.stderr


def test_missing_database_settings_are_reported(env):
    del env["POSTGRES_DB"]
    r = run("backup.sh", env)
    assert r.returncode != 0 and "POSTGRES_DB" in r.stderr


# ---------------- restore.sh ----------------


@pytest.fixture
def dump_file(tmp_path):
    f = tmp_path / "dating_bot_x.sql.gz"
    f.write_bytes(gzip.compress(DUMP.encode()))
    return f


def test_restore_requires_the_explicit_yes_flag(env, dump_file):
    r = run("restore.sh", env, str(dump_file))
    assert r.returncode == 2 and calls(env) == []


def test_restore_replays_the_dump_in_one_transaction_via_docker_exec(env, dump_file):
    r = run("restore.sh", env, str(dump_file), "--yes")
    assert r.returncode == 0, r.stderr
    log = calls(env)
    assert not [c for c in log if c.startswith("docker compose exec")]
    psql = [c for c in log if " psql " in c]
    assert len(psql) == 1 and psql[0].startswith("docker exec -i cid123 psql -U dating_bot")
    assert "--single-transaction" in psql[0] and "ON_ERROR_STOP=1" in psql[0]
    sent = Path(env["SHIM_PSQL_IN"]).read_text()
    assert sent.startswith("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    assert "secret_marker" in sent
    stop_i = log.index("docker compose stop bot")
    start_i = log.index("docker compose start bot")
    assert stop_i < log.index(psql[0]) < start_i


def test_failed_restore_leaves_the_bot_stopped_and_says_so(env, dump_file):
    env["SHIM_PSQL_EXIT"] = "3"
    r = run("restore.sh", env, str(dump_file), "--yes")
    assert r.returncode != 0 and "restore FAILED" in r.stderr
    assert "docker compose start bot" not in calls(env)


def test_restore_rejects_corrupt_files_before_touching_anything(env, tmp_path):
    bad = tmp_path / "bad.sql.gz"
    bad.write_text("not gzip")
    r = run("restore.sh", env, str(bad), "--yes")
    assert r.returncode == 2 and calls(env) == []


def test_restore_with_container_overrides_needs_no_compose(env, dump_file):
    env.update(SHIM_NO_PLUGIN="1", DB_CONTAINER="my-postgres", BOT_CONTAINER="my-bot")
    r = run("restore.sh", env, str(dump_file), "--yes")
    assert r.returncode == 0, r.stderr
    log = calls(env)
    assert "docker stop my-bot" in log and "docker start my-bot" in log
    assert any(c.startswith("docker exec -i my-postgres psql") for c in log)
