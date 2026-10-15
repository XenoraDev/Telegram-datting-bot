"""Forward-only SQL migration runner.

Rules (see README):
* Files live in ``migrations/`` and are named ``NNN_description.sql`` (``001``, ``002`` ...,
  contiguous, no gaps, no duplicates).
* ``001_init.sql`` creates ``schema_migrations``; every later file is recorded there with a
  sha256 checksum, in the same transaction as the migration itself.
* Don't put BEGIN/COMMIT in a file: the runner owns the transaction (rejected on discovery).
* An applied file must never be edited (checksum is verified) or removed.
* A cross-process advisory lock makes concurrent start-ups safe.
* Each migration runs in one transaction, so a failing file leaves nothing half-applied.
  (Statements that cannot run in a transaction, e.g. CREATE INDEX CONCURRENTLY, are not supported.)

Run standalone with ``python -m bot.db.migrate``.
"""

from __future__ import annotations

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


class MigrationError(RuntimeError):
    """Migration files or database state are inconsistent."""


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


async def migrate(pool: asyncpg.Pool, directory: Path | None = None) -> list[str]:
    """Apply all pending migrations. Returns the versions applied in this call."""
    migrations = discover(directory or DEFAULT_MIGRATIONS_DIR)
    by_version = {m.version: m for m in migrations}
    applied_now: list[str] = []

    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _ADVISORY_LOCK_KEY)
        try:
            applied = await _applied(conn)  # unqualified name: same search_path as the DDL below

            missing = sorted(set(applied) - set(by_version))
            if missing:
                raise MigrationError(
                    f"database has migrations that are not on disk: {missing} "
                    "(running older code against a newer schema?)"
                )
            for version, checksum in applied.items():
                if by_version[version].checksum != checksum:
                    raise MigrationError(
                        f"migration {version} was modified after being applied; "
                        "never edit applied migrations, add a new one instead"
                    )

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


async def _cli() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1, command_timeout=None)
    try:
        await migrate(pool)
    except MigrationError as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    finally:
        await pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_cli()))
