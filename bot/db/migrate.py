"""Forward-only SQL migration runner.

Rules (see README):
* Files live in ``migrations/`` and are named ``NNN_description.sql`` (``001``, ``002`` ...,
  contiguous, no gaps, no duplicates).
* ``001_init.sql`` creates ``schema_migrations``; every later file is recorded there with a
  sha256 checksum, in the same transaction as the migration itself.
* Don't put BEGIN/COMMIT in a file: the runner owns the transaction (rejected on discovery).
* An applied file must never be edited (checksum is verified) or removed.
* A cross-process advisory lock makes concurrent start-ups safe.
* Running OLDER code against a NEWER schema is refused, unless the release script sets
  ``ALLOW_NEWER_SCHEMA=1`` for an application rollback: then (and only then) migrations the code
  does not know are tolerated, provided they are exactly the next numbers after the newest file on
  disk (nothing missing in between) and every file this code knows is applied and unchanged.
  Nothing is ever applied or undone in that mode. Whether the newer schema is really safe for the
  older code is decided BEFORE the rollback (bot/db/compat.py), not here.
* Each migration runs in one transaction, so a failing file leaves nothing half-applied.
  (Statements that cannot run in a transaction, e.g. CREATE INDEX CONCURRENTLY, are not supported.)

Run standalone with ``python -m bot.db.migrate``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
# Arbitrary constant; the same for every process that migrates this database.
_ADVISORY_LOCK_KEY = 0x6D696772  # "migr"
_FILENAME_RE = re.compile(r"^(?P<number>\d{3})_(?P<name>[a-z0-9_]+)\.sql$")
# The runner owns the transaction. A top-level BEGIN/COMMIT/ROLLBACK inside a file would end it
# early and break the all-or-nothing guarantee. (plpgsql `BEGIN` has no semicolon, so function
# bodies are not matched.)
_TX_CONTROL_RE = re.compile(
    r"^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION)\s*;", re.IGNORECASE | re.MULTILINE
)


ALLOW_NEWER_ENV = "ALLOW_NEWER_SCHEMA"
_NUMBER_RE = re.compile(r"^(\d{3})_")


class MigrationError(RuntimeError):
    """Migration files or database state are inconsistent."""


def allow_newer_schema(env: dict[str, str] | None = None) -> bool:
    """True only for the exact value "1" (a rollback by the release script)."""
    return (env if env is not None else os.environ).get(ALLOW_NEWER_ENV, "") == "1"


def version_number(version: str) -> int | None:
    match = _NUMBER_RE.match(version)
    return int(match.group(1)) if match else None


def _unknown_newer(missing: list[str], migrations: list[Migration]) -> bool:
    """Are the applied-but-unknown versions exactly the next numbers after the newest file?"""
    numbers = sorted(n for v in missing if (n := version_number(v)) is not None)
    if len(numbers) != len(missing):
        return False
    first = migrations[-1].number + 1
    return numbers == list(range(first, first + len(numbers)))


@dataclass(frozen=True, slots=True)
class Migration:
    number: int
    version: str  # file stem, e.g. "001_init"
    sql: str
    checksum: str


def discover(directory: Path) -> list[Migration]:
    """Read and validate all migration files in ``directory``, ordered by number."""
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")

    found: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.name.startswith(".") or path.is_dir():
            continue
        match = _FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"invalid migration filename {path.name!r}; expected NNN_lower_snake_case.sql"
            )
        sql = path.read_text(encoding="utf-8")
        if _TX_CONTROL_RE.search(sql):
            raise MigrationError(
                f"{path.name}: remove BEGIN/COMMIT/ROLLBACK; each migration already runs "
                "in its own transaction"
            )
        found.append(
            Migration(
                number=int(match["number"]),
                version=path.stem,
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )

    found.sort(key=lambda m: m.number)
    if not found:
        raise MigrationError(f"no migration files in {directory}")
    for expected, migration in enumerate(found, start=1):
        if migration.number != expected:
            raise MigrationError(
                f"migration numbers must be contiguous from 001: expected "
                f"{expected:03d}, found {migration.version!r}"
            )
    return found


async def _applied(conn: asyncpg.Connection) -> dict[str, str]:
    """Return {version: checksum} of applied migrations ({} on a brand new database)."""
    if await conn.fetchval("SELECT to_regclass('schema_migrations')") is None:
        return {}
    rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
    return {r["version"]: r["checksum"] for r in rows}


async def migrate(
    pool: asyncpg.Pool, directory: Path | None = None, *, allow_newer: bool | None = None
) -> list[str]:
    """Apply all pending migrations. Returns the versions applied in this call.

    `allow_newer` (default: the ALLOW_NEWER_SCHEMA environment variable) lets older code start on
    a schema that a newer release extended; see the module docstring."""
    migrations = discover(directory or DEFAULT_MIGRATIONS_DIR)
    by_version = {m.version: m for m in migrations}
    applied_now: list[str] = []

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_KEY)
        try:
            applied = await _applied(conn)  # unqualified name: same search_path as the DDL below

            missing = sorted(set(applied) - set(by_version))
            tolerate = allow_newer if allow_newer is not None else allow_newer_schema()
            if missing and not tolerate:
                raise MigrationError(
                    f"database has migrations that are not on disk: {missing} "
                    "(running older code against a newer schema?)"
                )
            if missing and not _unknown_newer(missing, migrations):
                raise MigrationError(
                    f"database has migrations that are not on disk: {missing}, and they are not "
                    "simply the next numbers after the newest file here: refusing to run"
                )
            for version, checksum in applied.items():
                if version in by_version and by_version[version].checksum != checksum:
                    raise MigrationError(
                        f"migration {version} was modified after being applied; "
                        "never edit applied migrations, add a new one instead"
                    )
            if missing:
                older = [m.version for m in migrations if m.version not in applied]
                if older:  # never apply anything underneath a newer schema
                    raise MigrationError(
                        f"migrations {older} are not applied but newer ones are: refusing to run"
                    )
                log.warning(
                    "running against a NEWER schema (%s are applied but unknown to this release); "
                    "ALLOW_NEWER_SCHEMA is set, nothing is applied",
                    ", ".join(missing),
                )
                return []

            for migration in migrations:
                if migration.version in applied:
                    continue
                log.info("applying migration %s", migration.version)
                try:
                    async with conn.transaction():
                        await conn.execute(migration.sql)
                        await conn.execute(
                            "INSERT INTO schema_migrations (version, checksum) VALUES ($1, $2)",
                            migration.version,
                            migration.checksum,
                        )
                except asyncpg.PostgresError as exc:
                    raise MigrationError(f"migration {migration.version} failed: {exc}") from exc
                applied_now.append(migration.version)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_LOCK_KEY)

    if applied_now:
        log.info("applied %d migration(s): %s", len(applied_now), ", ".join(applied_now))
    else:
        log.info("database schema is up to date")
    return applied_now


@dataclass(frozen=True, slots=True)
class Status:
    """Read-only comparison of the migration files with what the database has applied."""

    latest_applied: str | None
    pending: tuple[str, ...]  # on disk, not applied
    unknown_newer: tuple[str, ...]  # applied, not on disk, exactly the next numbers (newer schema)
    unknown_other: tuple[str, ...]  # applied, not on disk, anything else (inconsistent)
    checksum_mismatch: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not (self.unknown_other or self.checksum_mismatch)

    def lines(self) -> list[str]:
        latest = self.latest_applied or ""
        number = version_number(latest) if latest else None
        return [
            f"latest_applied={latest}",
            f"latest_applied_number={'' if number is None else number}",
            f"pending={','.join(self.pending)}",
            f"unknown_newer={','.join(self.unknown_newer)}",
            f"unknown_other={','.join(self.unknown_other)}",
            f"checksum_mismatch={','.join(self.checksum_mismatch)}",
            f"ok={1 if self.ok else 0}",
        ]


async def status(pool: asyncpg.Pool, directory: Path | None = None) -> Status:
    """What `migrate` would find, without changing anything (no lock, no writes)."""
    migrations = discover(directory or DEFAULT_MIGRATIONS_DIR)
    by_version = {m.version: m for m in migrations}
    async with pool.acquire() as conn:
        applied = await _applied(conn)
    missing = sorted(set(applied) - set(by_version))
    newer = missing if _unknown_newer(missing, migrations) else []
    return Status(
        latest_applied=max(applied) if applied else None,
        pending=tuple(m.version for m in migrations if m.version not in applied),
        unknown_newer=tuple(newer),
        unknown_other=tuple(v for v in missing if v not in newer),
        checksum_mismatch=tuple(
            v for v, c in sorted(applied.items()) if v in by_version and by_version[v].checksum != c
        ),
    )


def info(directory: Path | None = None) -> list[str]:
    """What THIS code is, with no database: its newest migration and its release identity."""
    from .. import version  # imported late: the migrator itself needs nothing else from the app

    migrations = discover(directory or DEFAULT_MIGRATIONS_DIR)
    ident = version.current()
    return [
        f"latest_migration={migrations[-1].number}",
        f"migration_count={len(migrations)}",
        f"schema_tolerant={1 if version.SCHEMA_TOLERANT else 0}",
        f"version={ident.version}",
        f"sha={ident.sha}",
    ]


def _dsn() -> str | None:
    """DATABASE_URL, else built from POSTGRES_* exactly as the bot does (docker compose)."""
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn:
        return dsn
    from ..config import ConfigError, _database_url  # same builder (and percent-encoding)

    try:
        return _database_url(os.environ)
    except ConfigError:
        return None


async def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m bot.db.migrate", description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true", help="read-only: applied / pending")
    group.add_argument(
        "--info", action="store_true", help="no database: newest migration + release"
    )
    group.add_argument(
        "--plan",
        action="store_true",
        help="read-only: what the pending migrations mean for a later application rollback",
    )
    group.add_argument(
        "--check-rollback",
        metavar="N",
        type=int,
        help="read-only: can code whose newest migration is N run on this database?",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    if args.info:
        print("\n".join(info()))
        return 0
    dsn = _dsn()
    if not dsn:
        print(
            "DATABASE_URL (or POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB) is required",
            file=sys.stderr,
        )
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, command_timeout=None)
    try:
        if args.status:
            st = await status(pool)
            print("\n".join(st.lines()))
            return 0 if st.ok else 1
        if args.plan:
            from .compat import plan_pending

            plan = await plan_pending(pool)
            print("\n".join(plan.lines()))
            return 0 if plan.consistent else 1
        if args.check_rollback is not None:
            from .compat import check_rollback  # imported late: only the rollback gate needs it

            verdict = await check_rollback(pool, args.check_rollback)
            print("\n".join(verdict.lines()))
            return 0 if verdict.allowed else 3
        await migrate(pool)
    except MigrationError as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_cli()))
