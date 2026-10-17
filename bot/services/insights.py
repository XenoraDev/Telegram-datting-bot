"""Queue and funnel numbers for the admin "Queue & funnel" page.

Everything is an aggregate: the queue counters (migration 014) hold no user id, and the funnel is
counted from existing tables. It exists to answer one question with data: do people wait too long
for a random-chat partner (and so would more real users, or something else, help)?
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg

WINDOWS = (7, 30)  # days the page can show


def pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


class InsightsService:
    def __init__(self, pool: asyncpg.Pool, admin_ids: frozenset[int] = frozenset()) -> None:
        self._pool = pool
        self._admin_ids = admin_ids

    def is_admin(self, uid: int) -> bool:
        return uid in self._admin_ids

    async def report(self, admin_id: int, days: int = 7) -> dict | None:
        if not self.is_admin(admin_id):
            return None
        days = days if days in WINDOWS else WINDOWS[0]
        since = (datetime.now(UTC) - timedelta(days=days)).date()
        return {
            "days": days,
            "windows": WINDOWS,
            "queue": await self._queue(since),
            "hours": await self._hours(since),
            "daily": await self._daily(since),
            "funnel": await self._funnel(days),
        }

    async def _queue(self, since) -> dict:
        r = await self._pool.fetchrow(
            "SELECT coalesce(sum(instant), 0) AS instant, coalesce(sum(waited), 0) AS waited, "
            "coalesce(sum(wait_sum_seconds), 0) AS wait_sum, coalesce(sum(expired), 0) AS expired, "
            "coalesce(sum(cancelled), 0) AS cancelled FROM queue_stats WHERE day >= $1",
            since,
        )
        instant, waited, expired, cancelled = (
            int(r["instant"]),
            int(r["waited"]),
            int(r["expired"]),
            int(r["cancelled"]),
        )
        # a pairing ends two searches: the searcher at once (instant) and the waiter (waited)
        searches = instant + waited + expired + cancelled
        paired = instant + waited
        return {
            "searches": searches,
            "paired": paired,
            "paired_pct": pct(paired, searches),
            "instant_pct": pct(instant, searches),
            "expired": expired,
            "expired_pct": pct(expired, searches),
            "cancelled": cancelled,
            "cancelled_pct": pct(cancelled, searches),
            "avg_wait_s": round(int(r["wait_sum"]) / waited) if waited else None,
            "waited": waited,
        }

    async def _hours(self, since) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT hour, sum(instant + waited + expired + cancelled)::int AS searches, "
            "sum(expired)::int AS expired FROM queue_stats WHERE day >= $1 GROUP BY hour",
            since,
        )
        by_hour = {r["hour"]: r for r in rows}
        out = []
        for h in range(24):
            r = by_hour.get(h)
            s = r["searches"] if r else 0
            e = r["expired"] if r else 0
            ist = (h * 60 + 330) % 1440  # IST = UTC + 5:30
            out.append(
                {"hour": h, "ist": f"{ist // 60:02d}:{ist % 60:02d}", "searches": s, "expired": e}
            )
        return out

    async def _daily(self, since) -> list[dict]:
        rows = await self._pool.fetch(
            "SELECT day, sum(instant + waited + expired + cancelled)::int AS searches, "
            "sum(instant + waited)::int AS paired, sum(expired)::int AS expired "
            "FROM queue_stats WHERE day >= $1 GROUP BY day ORDER BY day DESC",
            since,
        )
        return [dict(r) for r in rows]

    async def _funnel(self, days: int) -> dict:
        """Of the real users who first wrote in the window, how many got how far (current state).
        Real = positive Telegram ids (load-test users are negative)."""
        r = await self._pool.fetchrow(
            """
            SELECT count(*) AS started,
                   count(*) FILTER (WHERE u.age_confirmed_at IS NOT NULL) AS age_confirmed,
                   count(*) FILTER (WHERE EXISTS (SELECT 1 FROM profiles p WHERE p.user_id = u.id))
                       AS profile_done,
                   count(*) FILTER (WHERE EXISTS (SELECT 1 FROM chat_session_members m
                                                  WHERE m.user_id = u.id)) AS first_chat,
                   count(*) FILTER (WHERE EXISTS (SELECT 1 FROM swipes s
                                                  WHERE s.swiper_id = u.id AND s.action = 'like'))
                       AS first_follow
            FROM users u
            WHERE u.id > 0 AND u.created_at >= now() - make_interval(days => $1)
            """,
            days,
        )
        started = int(r["started"])
        steps = (
            ("Started the bot", "started"),
            ("Confirmed 18+", "age_confirmed"),
            ("Finished the profile", "profile_done"),
            ("Had a random chat or a friend chat", "first_chat"),
            ("Followed someone", "first_follow"),
        )
        return {
            "started": started,
            "steps": [
                {"label": label, "n": int(r[key]), "pct": pct(int(r[key]), started)}
                for label, key in steps
            ],
        }
