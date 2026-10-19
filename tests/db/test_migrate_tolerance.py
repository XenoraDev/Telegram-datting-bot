"""Older code on a NEWER additive schema (an application rollback), and the read-only --status."""

import pytest

from bot.db import migrate as m
from bot.db.migrate import (
    DEFAULT_MIGRATIONS_DIR,
    MigrationError,
    allow_newer_schema,
    discover,
    info,
    migrate,
    status,
)
from tests.db.test_p14_migration import live

ALL = discover(DEFAULT_MIGRATIONS_DIR)
N = ALL[-1].number  # these tests follow however many migrations the release has
OLD = N - 2  # "older code" knows the files up to here ...
NEWER = tuple(x.version for x in ALL if x.number > OLD)  # ... and not these two
FIRST_NEWER = NEWER[0]


async def applied(pool):
    return [
        r["version"] for r in await pool.fetch("SELECT version FROM schema_migrations ORDER BY 1")
    ]


# ------------------------------------------------------------------ the default is unchanged


async def test_older_code_on_a_newer_schema_is_still_refused_by_default(app_pool, tmp_path):
    old = live(tmp_path, OLD)
    with pytest.raises(MigrationError, match="not on disk"):
        await migrate(app_pool, old)
    for flag in (False, None):
        with pytest.raises(MigrationError, match="not on disk"):
            await migrate(app_pool, old, allow_newer=flag)


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "True", " 1", "1 ", "2"])
def test_only_the_exact_value_one_turns_the_tolerance_on(value):
    assert allow_newer_schema({"ALLOW_NEWER_SCHEMA": value}) is False
    assert allow_newer_schema({}) is False
    assert allow_newer_schema({"ALLOW_NEWER_SCHEMA": "1"}) is True


async def test_the_environment_switch_is_what_the_bot_start_uses(app_pool, tmp_path, monkeypatch):
    old = live(tmp_path, OLD)
    monkeypatch.setenv("ALLOW_NEWER_SCHEMA", "yes")
    with pytest.raises(MigrationError):
        await migrate(app_pool, old)
    monkeypatch.setenv("ALLOW_NEWER_SCHEMA", "1")
    assert await migrate(app_pool, old) == []


# ------------------------------------------------------------------ what the tolerance allows


async def test_with_the_switch_older_code_starts_and_changes_nothing(app_pool, tmp_path, caplog):
    before = await applied(app_pool)
    rows = await app_pool.fetch("SELECT * FROM schema_migrations ORDER BY version")
    caplog.set_level("WARNING")
    assert await migrate(app_pool, live(tmp_path, OLD), allow_newer=True) == []
    assert await applied(app_pool) == before
    assert [
        dict(r) for r in await app_pool.fetch("SELECT * FROM schema_migrations ORDER BY 1")
    ] == [dict(r) for r in rows]
    assert "NEWER schema" in caplog.text and FIRST_NEWER in caplog.text


async def test_the_tolerance_is_only_for_the_next_numbers_after_the_newest_file(app_pool, tmp_path):
    old = live(tmp_path, OLD)
    await app_pool.execute(
        "DELETE FROM schema_migrations WHERE version = $1", FIRST_NEWER
    )  # a hole
    with pytest.raises(MigrationError, match="next numbers"):
        await migrate(app_pool, old, allow_newer=True)


async def test_an_unnumbered_unknown_entry_is_never_tolerated(app_pool, tmp_path):
    await app_pool.execute("INSERT INTO schema_migrations (version, checksum) VALUES ('zzz', 'x')")
    with pytest.raises(MigrationError, match="not simply the next numbers"):
        await migrate(app_pool, live(tmp_path, N), allow_newer=True)


async def test_a_changed_known_migration_is_still_refused_with_the_switch(app_pool, tmp_path):
    old = live(tmp_path, OLD)
    f = old / "003_profile_country.sql"
    f.write_text(f.read_text() + "\n-- edited after it was applied\n")
    with pytest.raises(MigrationError, match="modified after being applied"):
        await migrate(app_pool, old, allow_newer=True)


async def test_nothing_is_applied_underneath_a_newer_schema(app_pool, tmp_path):
    await app_pool.execute("DELETE FROM schema_migrations WHERE version = $1", ALL[OLD - 1].version)
    with pytest.raises(MigrationError, match="not applied but newer ones are"):
        await migrate(app_pool, live(tmp_path, OLD), allow_newer=True)
    assert ALL[OLD - 1].version not in await applied(app_pool)


async def test_the_switch_does_not_change_normal_forward_migrations(pool, tmp_path):
    assert len(await migrate(pool, live(tmp_path, OLD), allow_newer=True)) == OLD
    assert len(await migrate(pool, allow_newer=True)) == 2  # the two newest, on a database behind
    assert await migrate(pool, allow_newer=True) == []


# ------------------------------------------------------------------ --status (read-only)


async def test_status_of_an_empty_database_lists_everything_as_pending(pool):
    st = await status(pool)
    assert st.latest_applied is None and len(st.pending) == N and st.ok
    assert await pool.fetchval("SELECT to_regclass('schema_migrations')") is None  # untouched


async def test_status_of_an_up_to_date_database(app_pool):
    st = await status(app_pool)
    assert (st.pending, st.unknown_newer, st.unknown_other, st.checksum_mismatch) == ((),) * 4
    assert st.latest_applied == ALL[-1].version and st.ok
    assert st.lines() == [
        f"latest_applied={ALL[-1].version}",
        f"latest_applied_number={N}",
        "pending=",
        "unknown_newer=",
        "unknown_other=",
        "checksum_mismatch=",
        "ok=1",
    ]


async def test_status_of_a_database_that_is_behind(pool, tmp_path):
    await migrate(pool, live(tmp_path, OLD))
    st = await status(pool)
    assert st.pending == NEWER and st.ok
    assert f"pending={','.join(NEWER)}" in st.lines()


async def test_status_of_a_database_that_is_ahead(app_pool, tmp_path):
    st = await status(app_pool, live(tmp_path, OLD))
    assert st.unknown_newer == NEWER and st.ok
    assert st.unknown_other == () and st.pending == ()


async def test_status_reports_a_changed_file_and_an_odd_entry_as_not_ok(app_pool, tmp_path):
    old = live(tmp_path, N)
    (old / "003_profile_country.sql").write_text("-- changed\n")
    await app_pool.execute("INSERT INTO schema_migrations (version, checksum) VALUES ('zzz', 'x')")
    st = await status(app_pool, old)
    assert st.checksum_mismatch == ("003_profile_country",) and st.unknown_other == ("zzz",)
    assert not st.ok and "ok=0" in st.lines()


async def test_status_changes_nothing(app_pool, tmp_path):
    rows = [dict(r) for r in await app_pool.fetch("SELECT * FROM schema_migrations ORDER BY 1")]
    await status(app_pool)
    await status(app_pool, live(tmp_path, N - 5))
    assert [
        dict(r) for r in await app_pool.fetch("SELECT * FROM schema_migrations ORDER BY 1")
    ] == rows


# ------------------------------------------------------------------ --info and the command line


def test_info_needs_no_database_and_names_the_newest_migration():
    lines = info()
    assert lines[0] == f"latest_migration={N}" and f"migration_count={len(ALL)}" in lines
    assert "schema_tolerant=1" in lines and "version=dev" in lines and "sha=unknown" in lines


def test_the_database_url_is_built_from_the_postgres_variables_like_the_bot(monkeypatch):
    for key in (
        "DATABASE_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_HOST",
    ):
        monkeypatch.delenv(key, raising=False)
    assert m._dsn() is None
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/word")
    monkeypatch.setenv("POSTGRES_DB", "d")
    monkeypatch.setenv("POSTGRES_HOST", "db")
    assert m._dsn() == "postgresql://u:p%40ss%2Fword@db:5432/d"
    monkeypatch.setenv("DATABASE_URL", "postgresql://explicit/x")
    assert m._dsn() == "postgresql://explicit/x"


async def test_the_command_line_status_and_info(dsn, app_pool, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", dsn)
    assert await m._cli(["--info"]) == 0
    assert f"latest_migration={N}" in capsys.readouterr().out
    assert await m._cli(["--status"]) == 0
    assert "ok=1" in capsys.readouterr().out
    await app_pool.execute("INSERT INTO schema_migrations (version, checksum) VALUES ('zzz', 'x')")
    assert await m._cli(["--status"]) == 1
    assert "unknown_other=zzz" in capsys.readouterr().out
