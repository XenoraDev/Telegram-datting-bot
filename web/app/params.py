"""Strict parsing of query / form values. Anything odd becomes None, never an exception."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

MAX_ID = 2**53


def int_id(raw: str | None) -> int | None:
    s = (raw or "").strip()
    if s.isascii() and s.isdecimal() and 1 <= len(s) <= 15 and 1 <= int(s) < MAX_ID:
        return int(s)
    return None


def page_no(raw: str | None) -> int:
    s = (raw or "").strip()
    return int(s) - 1 if s.isascii() and s.isdecimal() and 1 <= len(s) <= 6 and int(s) >= 1 else 0


def day(raw: str | None) -> datetime | None:
    """YYYY-MM-DD as midnight UTC."""
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def next_day(d: datetime) -> datetime:
    return d + timedelta(days=1)
