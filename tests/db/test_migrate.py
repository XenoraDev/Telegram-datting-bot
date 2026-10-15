import asyncio
from pathlib import Path

import pytest

from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, MigrationError, discover, migrate

BOOKKEEPING = """
CREATE TABLE schema_migrations (
    version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def write(directory: Path, name: str, sql: str) -> None:
    (directory / name).write_text(sql, encoding="utf-8")


@pytest.fixture
def mdir(tmp_path: Path) -> Path:
    write(tmp_path, "001_init.sql", BOOKKEEPING)
    return tmp_path


# ---------- discover(): pure file validation, no database ----------


def test_real_migrations_directory_is_valid():
    migrations = discover(DEFAULT_MIGRATIONS_DIR)
    assert migrations[0].version == "001_init"
    assert [m.number for m in migrations] == list(range(1, len(migrations) + 1))


def test_discover_orders_by_number(mdir):
    write(mdir, "003_c.sql", "SELECT 3;")
    write(mdir, "002_b.sql", "SELECT 2;")
    assert [m.version for m in discover(mdir)] == ["001_init", "002_b", "003_c"]


def test_discover_rejects_gap(mdir):
    write(mdir, "003_c.sql", "SELECT 3;")
    with pytest.raises(MigrationError, match="contiguous"):
        discover(mdir)


def test_discover_rejects_must_start_at_001(tmp_path):
    write(tmp_path, "002_b.sql", "SELECT 2;")
    with pytest.raises(MigrationError, match="contiguous"):
        discover(tmp_path)


def test_discover_rejects_duplicate_numbers(mdir):
    write(mdir, "002_a.sql", "SELECT 1;")
    write(mdir, "002_b.sql", "SELECT 2;")
    with pytest.raises(MigrationError, match="contiguous"):
        discover(mdir)


@pytest.mark.parametrize("name", ["1_init.sql", "001-init.sql", "001_Init.sql", "001_init.txt"])
def test_discover_rejects_bad_filenames(mdir, name):
    write(mdir, name, "SELECT 1;")
    with pytest.raises(MigrationError, match="invalid migration filename"):
        discover(mdir)


def test_discover_rejects_empty_or_missing_dir(tmp_path):
    with pytest.raises(MigrationError, match="no migration files"):
        discover(tmp_path)
    with pytest.raises(MigrationError, match="not found"):
        discover(tmp_path / "missing")


def test_checksum_changes_with_content(tmp_path):
    write(tmp_path, "001_init.sql", "SELECT 1;")
    a = discover(tmp_path)[0].checksum
    write(tmp_path, "001_init.sql", "SELECT 2;")
    assert discover(tmp_path)[0].checksum != a


# ---------- migrate(): real PostgreSQL ----------


async def test_real_migrations_apply_from_scratch(pool):
    applied = await migrate(pool)
    assert applied == [m.version for m in discover(DEFAULT_MIGRATIONS_DIR)]
    rows = await pool.fetch("SELECT version, checksum FROM schema_migrations ORDER BY version")
    assert [r["version"] for r in rows] == applied
    assert all(len(r["checksum"]) == 64 for r in rows)


async def test_rerun_is_a_noop(pool, mdir):
    write(mdir, "002_t.sql", "CREATE TABLE t (id int);")
    assert await migrate(pool, mdir) == ["001_init", "002_t"]
    assert await migrate(pool, mdir) == []
    assert await pool.fetchval("SELECT count(*) FROM schema_migrations") == 2


async def test_only_new_migrations_are_applied_later(pool, mdir):
    await migrate(pool, mdir)
    write(mdir, "002_t.sql", "CREATE TABLE t (id int);")
    assert await migrate(pool, mdir) == ["002_t"]


async def test_multi_statement_migration(pool, mdir):
    write(
        mdir,
        "002_multi.sql",
        "CREATE TABLE a (id int);\nCREATE TABLE b (id int);\nINSERT INTO a VALUES (1);",
    )
    await migrate(pool, mdir)
    assert await pool.fetchval("SELECT count(*) FROM a") == 1
    assert await pool.fetchval("SELECT count(*) FROM b") == 0


async def test_modified_applied_migration_is_rejected(pool, mdir):
    write(mdir, "002_t.sql", "CREATE TABLE t (id int);")
    await migrate(pool, mdir)
    write(mdir, "002_t.sql", "CREATE TABLE t (id int, extra int);")
    with pytest.raises(MigrationError, match="modified after being applied"):
        await migrate(pool, mdir)


async def test_applied_migration_missing_on_disk_is_rejected(pool, mdir):
    write(mdir, "002_t.sql", "CREATE TABLE t (id int);")
    await migrate(pool, mdir)
    (mdir / "002_t.sql").unlink()
    with pytest.raises(MigrationError, match="not on disk"):
        await migrate(pool, mdir)


async def test_failing_migration_rolls_back_and_keeps_earlier_ones(pool, mdir):
    write(mdir, "002_ok.sql", "CREATE TABLE ok (id int);")
    write(mdir, "003_bad.sql", "CREATE TABLE half (id int);\nSELECT 1/0;")
    with pytest.raises(MigrationError, match="003_bad failed"):
        await migrate(pool, mdir)
    assert await pool.fetchval("SELECT to_regclass('ok')") is not None  # 002 stays applied
    assert await pool.fetchval("SELECT to_regclass('half')") is None  # 003 fully rolled back
    versions = {r["version"] for r in await pool.fetch("SELECT version FROM schema_migrations")}
    assert versions == {"001_init", "002_ok"}

    # Fix the broken file (it was never recorded, so editing it is legitimate) and retry.
    write(mdir, "003_bad.sql", "CREATE TABLE half (id int);")
    assert await migrate(pool, mdir) == ["003_bad"]


async def test_failing_first_migration_leaves_database_clean(pool, tmp_path):
    write(tmp_path, "001_init.sql", BOOKKEEPING + "SELECT 1/0;")
    with pytest.raises(MigrationError, match="001_init failed"):
        await migrate(pool, tmp_path)
    assert await pool.fetchval("SELECT to_regclass('schema_migrations')") is None


async def test_concurrent_runners_apply_each_migration_once(pool, mdir):
    write(mdir, "002_t.sql", "CREATE TABLE t (id int);")
    write(mdir, "003_u.sql", "CREATE TABLE u (id int);")
    results = await asyncio.gather(*(migrate(pool, mdir) for _ in range(4)))
    applied = [v for r in results for v in r]
    assert sorted(applied) == ["001_init", "002_t", "003_u"]  # exactly once in total
    assert await pool.fetchval("SELECT count(*) FROM schema_migrations") == 3


async def test_advisory_lock_is_released_after_failure(pool, mdir):
    write(mdir, "002_bad.sql", "SELECT 1/0;")
    with pytest.raises(MigrationError):
        await migrate(pool, mdir)
    write(mdir, "002_bad.sql", "SELECT 1;")
    # would hang forever if the lock leaked on some pooled connection
    assert await asyncio.wait_for(migrate(pool, mdir), timeout=5) == ["002_bad"]
