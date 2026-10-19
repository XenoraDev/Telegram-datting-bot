"""The migration compatibility manifest and the application-rollback gate (read-only)."""

import hashlib
import json
from pathlib import Path

import pytest

from bot.db import migrate as m
from bot.db.compat import (
    GUARDS,
    ManifestError,
    check_rollback,
    load_manifest,
    scan,
)
from bot.db.migrate import DEFAULT_MIGRATIONS_DIR, discover
from tests.db.test_p19_migration import APPLIED as APPLIED_001_018

ROOT = Path(__file__).resolve().parents[2]
FILES = {f.name: f for f in sorted(Path(DEFAULT_MIGRATIONS_DIR).glob("*.sql"))}
# sha256 of every migration file that has ever been applied. The tree this runs on may hold only
# the files up to some number (the release line without migrations 020 / 021 holds 001-019), so
# only the files that EXIST are compared; a pin for a file that is absent is allowed only when the
# file is NEWER than everything present (never a deleted older migration).
APPLIED = {
    **APPLIED_001_018,
    "019_support_live_reply.sql": (
        "9d2343cc8f472df182c57ae4f36239143ec1bb9c364a40913a877ef6768ff790"
    ),
    "020_sub_shorten.sql": ("102d11bc74446ea8fe9426ff361f552ca5a59c222eb0e9a24f922f2b69a913f7"),
    "021_traffic_attribution.sql": (
        "eba36521633b7f0497a54474d564432a828e9832f9609905de7a49bc182569d5"
    ),
}
PRESENT = sorted(discover(DEFAULT_MIGRATIONS_DIR), key=lambda x: x.number)
N = PRESENT[-1].number  # the newest migration THIS tree has

# Reviewed by hand against the actual SQL (see the notes in release/migration_compat.json).
# A change here is a deliberate decision, never an accident.
REVIEWED = {
    1: "expand", 2: "expand", 3: "expand", 4: "expand", 5: "expand",
    6: "contract",     # users.language CHECK narrows an existing column
    7: "expand",
    8: "contract",     # rewrites plan_entitlements data
    9: "expand", 10: "expand", 11: "expand", 12: "expand", 13: "expand", 14: "expand",
    15: "expand", 16: "expand", 17: "expand", 18: "expand",
    19: "conditional",  # old code's ticket close violates the live_until CHECK
    20: "expand", 21: "expand",
}  # fmt: skip


# ------------------------------------------------------------------ the manifest


def test_the_historical_migrations_are_byte_identical():
    for name, digest in APPLIED.items():
        if name in FILES:
            assert hashlib.sha256(FILES[name].read_bytes()).hexdigest() == digest, name


def test_only_migrations_newer_than_this_tree_may_be_absent():
    for name in set(APPLIED) - set(FILES):
        assert int(name[:3]) > N, f"{name} was applied once and is missing from this tree"
    assert set(FILES) >= {name for name in APPLIED if int(name[:3]) <= N}


def test_every_existing_migration_is_classified():
    manifest = load_manifest()
    for migration in PRESENT:
        assert migration.number in manifest, f"{migration.version} has no classification"


def test_the_manifest_may_only_list_extra_migrations_that_come_after_this_tree():
    extra = set(load_manifest()) - {x.number for x in PRESENT}
    assert all(number > N for number in extra), extra  # future migrations only, never a gap


def test_the_classification_is_the_reviewed_one():
    manifest = load_manifest()
    assert {n: e.klass for n, e in manifest.items() if n in REVIEWED} == REVIEWED
    # anything newer than what was reviewed must at least be a valid class
    assert all(e.klass in ("expand", "conditional", "contract") for e in manifest.values())


def test_the_conditional_migrations_name_a_real_guard():
    for n, e in load_manifest().items():
        assert (e.guard is not None) == (e.klass == "conditional"), n
        assert e.guard is None or e.guard in GUARDS


def test_the_lint_agrees_with_the_manifest_for_every_migration():
    manifest = load_manifest()
    for name, path in FILES.items():
        number = int(name[:3])
        found = scan(path.read_text())
        entry = manifest[number]
        if found["hard"]:  # removes / renames / retypes / rewrites data: never "expand"
            assert entry.klass != "expand", (name, found["hard"])
        if found["constraint"] and entry.klass == "expand":
            assert "CHECK" in entry.note, (name, found["constraint"])  # was reviewed, said so


def test_the_expand_only_migrations_contain_nothing_destructive():
    manifest = load_manifest()
    for name, path in FILES.items():
        if manifest[int(name[:3])].klass == "expand":
            assert scan(path.read_text())["hard"] == [], name


# ------------------------------------------------------------------ the lint itself


@pytest.mark.parametrize(
    "sql, hard",
    [
        ("ALTER TABLE t DROP COLUMN x;", ["DROP COLUMN"]),
        ("DROP TABLE t;", ["DROP TABLE"]),
        ("ALTER TABLE t RENAME COLUMN a TO b;", ["RENAME"]),
        ("ALTER TABLE t ALTER COLUMN a SET NOT NULL;", ["SET NOT NULL"]),
        ("ALTER TABLE t ALTER COLUMN a TYPE bigint;", ["ALTER COLUMN TYPE"]),
        ("ALTER TABLE t ADD COLUMN a INT NOT NULL;", ["ADD COLUMN NOT NULL without DEFAULT"]),
        ("UPDATE t SET a = 1;", ["UPDATE (data rewrite)"]),
        ("DELETE FROM t;", ["DELETE (data rewrite)"]),
        ("TRUNCATE t;", ["TRUNCATE"]),
        ("ALTER TABLE t DROP CONSTRAINT some_check;", ["DROP CONSTRAINT some_check"]),
    ],
)
def test_the_lint_flags_destructive_statements(sql, hard):
    assert scan(sql)["hard"] == hard


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE t ADD COLUMN a INT;",
        "ALTER TABLE t ADD COLUMN a INT NOT NULL DEFAULT 0;",
        "ALTER TABLE t ADD COLUMN a TEXT NOT NULL DEFAULT 'x' CHECK (a <> '');",
        "CREATE TABLE t (id INT NOT NULL);",
        "CREATE INDEX i ON t (a);",
        "-- DROP TABLE in a comment\nSELECT 1;",
        "ALTER TABLE admin_actions DROP CONSTRAINT admin_actions_action_check;",
    ],
)
def test_the_lint_passes_additive_statements(sql):
    assert scan(sql) == {"hard": [], "constraint": []}


def test_a_new_constraint_is_reported_but_the_widened_action_check_is_not():
    assert scan("ALTER TABLE t ADD CONSTRAINT a_check CHECK (a > 0);")["constraint"] == ["a_check"]
    assert (
        scan("ALTER TABLE admin_actions ADD CONSTRAINT admin_actions_action_check CHECK (true);")[
            "constraint"
        ]
        == []
    )


# ------------------------------------------------------------------ manifest validation


def write(tmp_path, rows, key="migrations"):
    f = tmp_path / "m.json"
    f.write_text(json.dumps({key: rows}))
    return f


GOOD = {"class": "expand", "note": "a long enough explanation of why this is fine"}


@pytest.mark.parametrize(
    "rows",
    [
        {},
        {"1": GOOD},
        {"001": {"class": "safe", "note": GOOD["note"]}},
        {"001": {"class": "expand"}},
        {"001": {"class": "expand", "note": "short"}},
        {"001": {"class": "conditional", "note": GOOD["note"]}},
        {"001": {"class": "conditional", "guard": "nope", "note": GOOD["note"]}},
        {"001": {"class": "expand", "guard": "support_live_cleared", "note": GOOD["note"]}},
        {"001": "expand"},
    ],
)
def test_a_malformed_manifest_is_refused(tmp_path, rows):
    with pytest.raises(ManifestError):
        load_manifest(write(tmp_path, rows))


def test_a_missing_or_broken_manifest_file_is_refused(tmp_path):
    with pytest.raises(ManifestError):
        load_manifest(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(ManifestError):
        load_manifest(bad)


# ------------------------------------------------------------------ the rollback gate


async def live_ticket(pool):
    await pool.execute(
        "INSERT INTO users (id, status, age_confirmed_at) VALUES (1, 'active', now())"
    )
    await pool.execute(
        "INSERT INTO support_tickets (user_id, category, live_until) "
        "VALUES (1, 'other', now() + interval '10 minutes')"
    )


async def test_nothing_newer_means_nothing_to_check(app_pool):
    verdict = await check_rollback(app_pool, N)
    assert verdict.allowed and verdict.newer == () and verdict.blocked == ()
    assert verdict.lines() == ["allowed=1", f"target={N}", "newer="]
    assert (await check_rollback(app_pool, 99)).allowed  # a newer target than the database


async def test_only_additive_migrations_in_between_allow_the_rollback(app_pool):
    verdict = await check_rollback(app_pool, 17)  # 018 expand, 019 conditional (guard clear), ...
    assert verdict.allowed and verdict.newer == tuple(range(18, N + 1))
    newer = ",".join(f"{n:03d}" for n in range(18, N + 1))
    assert verdict.lines()[:3] == ["allowed=1", "target=17", f"newer={newer}"]


@pytest.mark.skipif(N < 21, reason="this tree has no migrations 020 / 021")
async def test_020_and_021_are_expand_so_a_rollback_to_019_is_allowed(app_pool):
    verdict = await check_rollback(app_pool, 19)
    assert verdict.allowed and verdict.newer == (20, 21)
    assert verdict.lines()[:3] == ["allowed=1", "target=19", "newer=020,021"]


async def test_the_conditional_migration_passes_while_its_guard_is_clear(app_pool):
    assert (await check_rollback(app_pool, 18)).allowed  # 019 (guard clear) 020 021


async def test_the_conditional_migration_blocks_while_a_ticket_is_live(app_pool):
    await live_ticket(app_pool)
    verdict = await check_rollback(app_pool, 18)
    assert not verdict.allowed
    assert "019 is blocked by guard support_live_cleared (1)" in verdict.blocked[0]
    assert (await check_rollback(app_pool, 19)).allowed  # a target that knows 019 is unaffected


async def test_a_contract_migration_blocks_the_rollback_with_its_note(app_pool):
    verdict = await check_rollback(app_pool, 7)  # 008 rewrites data
    assert not verdict.allowed
    assert any(b.startswith("008 is a contract migration") for b in verdict.blocked)
    assert not any(b.startswith("006") for b in verdict.blocked)  # 006 is older than the target
    verdict = await check_rollback(app_pool, 5)
    assert any(b.startswith("006 is a contract") for b in verdict.blocked)


async def test_an_unclassified_migration_is_treated_as_a_contract(app_pool):
    manifest = load_manifest()
    del manifest[N]
    verdict = await check_rollback(app_pool, N - 1, manifest)
    assert not verdict.allowed
    assert f"{N:03d} has no compatibility classification" in verdict.blocked[0]


async def test_the_gate_changes_nothing(app_pool):
    await live_ticket(app_pool)
    before = [dict(r) for r in await app_pool.fetch("SELECT * FROM schema_migrations ORDER BY 1")]
    tickets = [dict(r) for r in await app_pool.fetch("SELECT * FROM support_tickets")]
    for target in (5, 7, 18, N):
        await check_rollback(app_pool, target)
    assert [
        dict(r) for r in await app_pool.fetch("SELECT * FROM schema_migrations ORDER BY 1")
    ] == before
    assert [dict(r) for r in await app_pool.fetch("SELECT * FROM support_tickets")] == tickets


async def test_the_command_line_exit_codes(dsn, app_pool, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", dsn)
    assert await m._cli(["--check-rollback", "19"]) == 0
    assert "allowed=1" in capsys.readouterr().out
    assert await m._cli(["--check-rollback", "7"]) == 3  # refused
    out = capsys.readouterr().out
    assert "allowed=0" in out and "blocked=008 is a contract" in out
    with pytest.raises(SystemExit) as exit_:  # the options exclude each other
        await m._cli(["--check-rollback", "7", "--status"])
    assert exit_.value.code == 2


# ------------------------------------------------------------------ --plan (pending migrations)


async def test_the_plan_of_an_up_to_date_database_is_empty_and_safe(app_pool):
    from bot.db.compat import plan_pending

    plan = await plan_pending(app_pool)
    assert plan.pending == () and plan.rollback_safe and plan.consistent
    assert plan.lines() == [
        "pending=",
        "pending_contract=",
        "pending_conditional=",
        "pending_unlisted=",
        "consistent=1",
        "rollback_safe=1",
    ]


async def test_pending_additive_migrations_are_rollback_safe(pool, tmp_path):
    from bot.db.compat import plan_pending
    from tests.db.test_p14_migration import live

    await m.migrate(pool, live(tmp_path, 18))
    plan = await plan_pending(pool)
    assert plan.pending == tuple(
        x.version for x in discover(DEFAULT_MIGRATIONS_DIR) if x.number > 18
    )
    assert plan.conditional == ("019_support_live_reply",) and plan.rollback_safe


async def test_a_pending_contract_or_unlisted_migration_makes_the_release_not_rollback_safe(
    pool, tmp_path
):
    from bot.db.compat import plan_pending
    from tests.db.test_p14_migration import live

    await m.migrate(pool, live(tmp_path, 4))  # 006 (contract) and 008 (contract) are pending
    plan = await plan_pending(pool)
    assert plan.contract == ("006_language_entitlements", "008_tiers_privacy")
    assert not plan.rollback_safe
    manifest = load_manifest()
    del manifest[N]
    await m.migrate(pool, live(tmp_path, N - 1, "live_prev"))  # everything but the newest
    unlisted = await plan_pending(pool, manifest)
    assert unlisted.unlisted == (PRESENT[-1].version,) and not unlisted.rollback_safe


async def test_the_command_line_plan(dsn, app_pool, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", dsn)
    assert await m._cli(["--plan"]) == 0
    assert "rollback_safe=1" in capsys.readouterr().out
