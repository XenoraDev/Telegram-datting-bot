"""The backup / off-server-copy runbook and the deploy guidance: they exist, agree with the scripts
and the README, carry no secrets, and the one-line checks they give actually work."""

import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNBOOK = (ROOT / "docs/BACKUP_RUNBOOK.md").read_text()
README = (ROOT / "README.md").read_text()
CRON = "15 3 * * * cd /path/to/dating-bot-v2 && ./scripts/backup.sh >> backups/backup.log 2>&1"


def test_the_runbook_covers_every_step():
    for heading in (
        "What is backed up",
        "Run it by hand first",
        "Schedule it (daily)",
        "Copy it off the server",
        "Know when it stops working",
        "Practise the restore",
        "If you ever restore production",
        "Checklist",
    ):
        assert re.search(rf"^## (\d\. )?{re.escape(heading)}", RUNBOOK, re.MULTILINE), heading
    for needle in ("rsync", "rclone", "aaPanel", "encrypt", "staging", "BACKUP_KEEP_DAYS"):
        assert needle in RUNBOOK, needle


def test_it_says_the_repository_configures_nothing():
    assert "Nothing here is configured by the" in RUNBOOK and "repository" in RUNBOOK
    assert not any(
        (ROOT / p).exists() for p in ("scripts/offsite-copy.sh", "scripts/install-cron.sh")
    )


def test_the_scripts_and_settings_it_names_exist_and_agree():
    for script in ("backup.sh", "restore.sh"):
        path = ROOT / "scripts" / script
        assert path.exists() and os.access(path, os.X_OK), script
        assert f"./scripts/{script}" in RUNBOOK
    assert CRON in RUNBOOK
    for text in (RUNBOOK, README):  # the same daily line, whatever the path placeholder
        assert "15 3 * * * cd /path/to/" in text
        assert "./scripts/backup.sh >> backups/backup.log 2>&1" in text
    assert (
        "15 3 * * * cd /path/to/dating-bot-v2 && ./scripts/backup.sh"
        in (ROOT / "scripts/backup.sh").read_text()
    )
    default = re.search(
        r'BACKUP_KEEP_DAYS="\$\{BACKUP_KEEP_DAYS:-(\d+)\}"',
        (ROOT / "scripts/backup.sh").read_text(),
    )
    assert default and f"default {default.group(1)}" in RUNBOOK
    assert f"BACKUP_KEEP_DAYS={default.group(1)}" in (ROOT / ".env.example").read_text()
    assert "docs/BACKUP_RUNBOOK.md" in README


def test_the_runbook_contains_no_secret_or_real_address():
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", RUNBOOK)  # no IP addresses
    assert not re.search(r"(?i)(password|secret|token|api[_-]?key)\s*[=:]\s*\S{6,}", RUNBOOK)
    assert not re.search(r"AKIA[0-9A-Z]{12,}|-----BEGIN|\d{8,}:[A-Za-z0-9_-]{30,}", RUNBOOK)
    for host in re.findall(r"@([a-z0-9.-]+):", RUNBOOK):
        assert host in {"other-host"}, host  # only placeholders


def test_the_freshness_check_exits_zero_with_a_recent_backup_and_nonzero_without(tmp_path):
    snippet = next(
        line.strip() for line in RUNBOOK.splitlines() if line.startswith("find /path/to/")
    )
    backups = tmp_path / "backups"
    backups.mkdir()
    command = snippet.replace("/path/to/dating-bot-v2/backups", str(backups))

    def run() -> int:
        return subprocess.run(["bash", "-c", command], check=False).returncode  # noqa: S603, S607

    assert run() != 0  # nothing at all
    old = backups / "db_old.sql.gz"
    old.write_bytes(b"x")
    three_days_ago = time.time() - 3 * 86400
    os.utime(old, (three_days_ago, three_days_ago))
    assert run() != 0  # only a stale one
    (backups / "db_new.sql.gz").write_bytes(b"x")
    assert run() == 0


def test_the_deploy_guidance_is_in_the_readme():
    section = README[README.index("### Restarts and off-peak deploys") :]
    for needle in (
        "Survives",
        "Is lost",
        "quietest hour",
        "./scripts/backup.sh",
        "starting long polling",
        "QUEUE_TIMEOUT_MINUTES",
        "ADMIN_IDS",
    ):
        assert needle in section, needle
    assert "QUEUE_TIMEOUT_MINUTES" in (ROOT / ".env.example").read_text()
    assert "012_support_tickets" in README
