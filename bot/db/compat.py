"""Can OLDER application code run on the database as it is now? (the application-rollback gate)

An application rollback restores the previous release's image and keeps the database as it is:
a database is never rolled back automatically. That is only safe if every migration the older code
does not know is backward compatible for it. ``release/migration_compat.json`` classifies every
migration (the historical files are immutable, so the classification lives beside them):

* ``expand``       additive: older code keeps working (new tables, defaulted / nullable columns,
                   widened CHECKs, new indexes);
* ``conditional``  compatible only while a read-only GUARD (below) finds nothing in the way;
* ``contract``     removes, renames, narrows or rewrites something: older code may break.

A migration with no entry is treated as ``contract``. The gate only ever answers yes or no; it
changes nothing. ``python -m bot.db.migrate --check-rollback N`` runs it (N = the newest migration
the target release knows).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from .migrate import _applied, version_number

MANIFEST_FILE = Path(__file__).resolve().parents[2] / "release" / "migration_compat.json"
CLASSES = ("expand", "conditional", "contract")

# name -> a read-only query returning one number; the rollback is allowed only when it is 0.
GUARDS: dict[str, str] = {
    # older code closes a ticket without clearing live_until (migration 019's CHECK refuses that)
    "support_live_cleared": "SELECT count(*) FROM support_tickets WHERE live_until IS NOT NULL",
}


class ManifestError(ValueError):
    """release/migration_compat.json is malformed."""


@dataclass(frozen=True, slots=True)
class Entry:
    number: int
    klass: str
    note: str
    guard: str | None = None


def load_manifest(path: Path | str | None = None) -> dict[int, Entry]:
    try:
        raw = json.loads(Path(path or MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read the migration compatibility manifest: {exc}") from exc
    rows = raw.get("migrations") if isinstance(raw, dict) else None
    if not isinstance(rows, dict) or not rows:
        raise ManifestError("the manifest has no 'migrations' table")
    out: dict[int, Entry] = {}
    for key, row in rows.items():
        if not re.fullmatch(r"\d{3}", key):
            raise ManifestError(f"bad migration number {key!r}")
        if not isinstance(row, dict) or row.get("class") not in CLASSES:
            raise ManifestError(f"{key}: 'class' must be one of {CLASSES}")
        note = row.get("note")
        if not isinstance(note, str) or len(note.strip()) < 20:
            raise ManifestError(f"{key}: every classification needs a real 'note'")
        guard = row.get("guard")
        if (row["class"] == "conditional") != (guard is not None) or (
            guard is not None and guard not in GUARDS
        ):
            raise ManifestError(f"{key}: 'conditional' needs exactly one known 'guard'")
        out[int(key)] = Entry(int(key), row["class"], note.strip(), guard)
    return out


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    target: int  # the newest migration the older release knows
    newer: tuple[int, ...]  # applied migrations that release does not know
    blocked: tuple[str, ...]  # why not, one line each

    def lines(self) -> list[str]:
        return [
            f"allowed={1 if self.allowed else 0}",
            f"target={self.target}",
            f"newer={','.join(f'{n:03d}' for n in self.newer)}",
            *[f"blocked={b}" for b in self.blocked],
        ]


async def check_rollback(
    pool: asyncpg.Pool, target_latest: int, manifest: dict[int, Entry] | None = None
) -> Verdict:
    """Read-only: may code whose newest migration is `target_latest` run on this database?"""
    entries = manifest if manifest is not None else load_manifest()
    async with pool.acquire() as conn:
        applied = await _applied(conn)
        numbers = sorted({n for v in applied if (n := version_number(v)) is not None})
        newer = tuple(n for n in numbers if n > target_latest)
        blocked: list[str] = []
        for n in newer:
            entry = entries.get(n)
            if entry is None:
                blocked.append(f"{n:03d} has no compatibility classification (treated as contract)")
            elif entry.klass == "contract":
                blocked.append(f"{n:03d} is a contract migration: {entry.note}")
            elif entry.klass == "conditional":
                count = int(await conn.fetchval(GUARDS[entry.guard]) or 0)
                if count:
                    blocked.append(
                        f"{n:03d} is blocked by guard {entry.guard} ({count}): {entry.note}"
                    )
    return Verdict(not blocked, target_latest, newer, tuple(blocked))


# ---------------------------------------------------------------- a lint for the manifest

_COMMENT = re.compile(r"--[^\n]*")
_ALTER_TYPE = re.compile(r"ALTER\s+COLUMN\s+\w+\s+(SET\s+DATA\s+)?TYPE", re.IGNORECASE)
_NOT_NULL_NO_DEFAULT = re.compile(
    r"ADD\s+COLUMN\s+\w+\s+[^,;]*?\bNOT\s+NULL\b(?![^,;]*\bDEFAULT\b)", re.IGNORECASE
)


def scan(sql: str) -> dict[str, list[str]]:
    """What a migration file does that could break older code (a review aid, not a verdict).

    ``hard``: removes / renames / retypes / rewrites; ``constraint``: adds a constraint that is not
    the (always widening) admin_actions action CHECK."""
    text = _COMMENT.sub("", sql)
    upper = text.upper()
    hard: list[str] = []
    for token in ("DROP COLUMN", "DROP TABLE", "RENAME", "SET NOT NULL", "TRUNCATE"):
        if token in upper:
            hard.append(token)
    if _ALTER_TYPE.search(text):
        hard.append("ALTER COLUMN TYPE")
    if _NOT_NULL_NO_DEFAULT.search(text):
        hard.append("ADD COLUMN NOT NULL without DEFAULT")
    for line in upper.splitlines():
        stripped = line.strip()
        if stripped.startswith(("UPDATE ", "DELETE FROM ")):
            hard.append(stripped.split()[0] + " (data rewrite)")
            break
    widened = "admin_actions_action_check"
    constraint = [
        m.group(1)
        for m in re.finditer(r"ADD\s+CONSTRAINT\s+(\w+)", text, re.IGNORECASE)
        if m.group(1).lower() != widened
    ]
    # dropping a constraint other than the widened action CHECK is also a change of rules
    for m in re.finditer(r"DROP\s+CONSTRAINT\s+(\w+)", text, re.IGNORECASE):
        if m.group(1).lower() != widened:
            hard.append(f"DROP CONSTRAINT {m.group(1)}")
    return {"hard": sorted(set(hard)), "constraint": constraint}
