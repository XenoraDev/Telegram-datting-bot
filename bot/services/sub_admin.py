"""Subscription management for admins (S2): cancel a subscription, revoke a payment's approval.

Every call needs an admin id from ADMIN_IDS, a confirmation flag and a written reason, runs in ONE
transaction with the subscription row locked, and writes its audit entry in that same transaction:
a failed or refused call leaves no audit entry, and a double submit changes nothing the second time.
Nothing is deleted. The subscription keeps its dates; only its status becomes `canceled`. A payment
row is never touched and no refund is processed or implied.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import asyncpg

from ..db import repo_subscriptions as repo
from .payments import MIN_REASON, clean_text
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

MAX_REASON = 300
MAX_SHORTEN_DAYS = 3660
MIN_LEFT = timedelta(hours=1)  # a subscription is never shortened to less than this from now


@dataclass(frozen=True, slots=True)
class SubResult:
    # ok | denied | rate_limited | confirm_required | reason_required | not_found | not_active |
    # not_approved | no_subscription | ambiguous | already_ended
    kind: str
    user_id: int | None = None
    subscription_id: int | None = None
    payment_id: int | None = None
    plan_name: str | None = None
    phase: str | None = None  # running | queued: what was cancelled
    reference: str | None = None  # revoke: the payment's reference
    starts_at: object | None = None
    ends_at: object | None = None  # shorten / grant: the subscription's new end
    days: int | None = None
    shifted: int = 0  # shorten: queued subscriptions moved earlier
    mode: str | None = None  # grant: started | extended | upgraded | queued
    max_days: int | None = None  # too_much: the most that can be taken off


@dataclass(frozen=True, slots=True)
class Shift:
    """One queued subscription moving with a shortened one."""

    id: int
    plan_name: str
    old_start: object
    old_end: object
    new_start: object
    new_end: object


@dataclass(frozen=True, slots=True)
class ShortenCheck:
    """What taking `days` off a subscription would do, or why it can not be done. Read-only."""

    problem: str | None  # None = can be done
    sub: asyncpg.Record | None = None
    payment: asyncpg.Record | None = None  # set when a payment's extension is being removed
    days: int = 0
    old_end: object | None = None
    new_end: object | None = None
    max_days: int = 0
    shifts: tuple[Shift, ...] = ()
    removed_by: asyncpg.Record | None = None  # an earlier removal of the same payment
    expected: str = ""  # the end date the admin saw; the change only applies if it is still this


@dataclass(frozen=True, slots=True)
class RevokeCheck:
    """What a revoke of one payment's approval would do (or why it can not be done)."""

    problem: str | None  # None = can be revoked
    subscription: asyncpg.Record | None = None
    revoked_by: asyncpg.Record | None = None  # an earlier revoke, if any


class SubscriptionAdminService:
    def __init__(self, pool: asyncpg.Pool, limiter: RateLimiter, admin_ids: frozenset[int]) -> None:
        self._pool = pool
        self._limiter = limiter
        self._admin_ids = admin_ids

    def is_admin(self, uid: int) -> bool:
        return uid in self._admin_ids

    def _gate(self, admin_id: int) -> SubResult | None:
        if not self.is_admin(admin_id):
            return SubResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return SubResult("rate_limited")
        return None

    @staticmethod
    def _checks(confirmed: bool, reason: str | None) -> tuple[SubResult | None, str]:
        if not confirmed:
            return SubResult("confirm_required"), ""
        text = clean_text(reason, MAX_REASON)
        if len(text) < MIN_REASON:
            return SubResult("reason_required"), ""
        return None, text

    # ------------------------------------------------------------ cancel one subscription

    async def preview_cancel(self, admin_id: int, sub_id: int) -> dict | None:
        """What the confirmation page shows. None for a non-admin or an unknown subscription."""
        if not self.is_admin(admin_id):
            return None
        sub = await repo.get_subscription(self._pool, sub_id)
        if sub is None:
            return None
        return {"sub": sub, "others": await repo.live_others(self._pool, sub["user_id"], sub_id)}

    async def cancel(
        self, admin_id: int, sub_id: int, reason: str | None, *, confirmed: bool
    ) -> SubResult:
        """Cancel a running or queued subscription (status -> canceled; the row stays)."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        refusal, text = self._checks(confirmed, reason)
        if refusal is not None:
            return refusal
        async with self._pool.acquire() as conn, conn.transaction():
            sub = await repo.get_subscription(conn, sub_id, lock=True)
            if sub is None:
                return SubResult("not_found")
            if sub["phase"] == "ended" or not await repo.cancel(conn, sub_id, sub["user_id"]):
                return SubResult("not_active", sub["user_id"], sub_id, plan_name=sub["plan_name"])
            await repo.audit(
                conn,
                admin_id,
                "sub_cancel",
                user_id=sub["user_id"],
                subscription_id=sub_id,
                payment_id=None,
                reason=text,
                detail=f"{sub['plan_name']} ({sub['phase']}) ends {_day(sub['current_period_end'])}"
                f": {text}",
            )
        log.info("subscription canceled by an admin")
        return SubResult(
            "ok",
            sub["user_id"],
            sub_id,
            plan_name=sub["plan_name"],
            phase=sub["phase"],
            starts_at=sub["started_at"],
        )

    # ------------------------------------------------------------ revoke a payment's approval

    async def check_revoke(self, admin_id: int, payment_id: int) -> RevokeCheck | None:
        """Can this payment's approval be revoked? Read-only, for the payment page and the
        confirmation page. None for a non-admin."""
        if not self.is_admin(admin_id):
            return None
        return await self._revoke_check(self._pool, payment_id, lock=False)

    @staticmethod
    async def _revoke_check(db, payment_id: int, *, lock: bool) -> RevokeCheck:
        """The relationship is only accepted when it is unambiguous: the payment is approved, the
        subscription it points to belongs to the same user, was CREATED by this very payment
        (its provider reference is the payment's reference) and no other approved payment
        points at it. Anything else (missing, shared, extended) is refused, never guessed."""
        payment = await repo.payment_with_plan(db, payment_id)
        if payment is None:
            return RevokeCheck("not_found")
        if payment["status"] != "approved":
            return RevokeCheck("not_approved")
        revoked = await repo.revoke_audit(db, payment_id)
        if payment["subscription_id"] is None:
            return RevokeCheck("no_subscription", revoked_by=revoked)
        sub = await repo.get_subscription(db, payment["subscription_id"], lock=lock)
        if sub is None or sub["user_id"] != payment["user_id"]:
            return RevokeCheck("no_subscription", revoked_by=revoked)
        if (
            sub["provider_ref"] != payment["reference"]
            or await repo.approved_payments_pointing_at(db, sub["id"]) != 1
        ):
            return RevokeCheck("ambiguous", sub, revoked)
        if await repo.changed_by_admin(db, sub["id"]):
            return RevokeCheck("ambiguous", sub, revoked)  # time was granted / taken off by hand
        if sub["phase"] == "ended":
            return RevokeCheck("already_ended", sub, revoked)
        return RevokeCheck(None, sub, revoked)

    async def revoke_payment(
        self, admin_id: int, payment_id: int, reason: str | None, *, confirmed: bool
    ) -> SubResult:
        """Withdraw an approved payment's subscription (cancel exactly that subscription)."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        refusal, text = self._checks(confirmed, reason)
        if refusal is not None:
            return refusal
        async with self._pool.acquire() as conn, conn.transaction():
            check = await self._revoke_check(conn, payment_id, lock=True)
            if check.problem is not None:
                return SubResult(check.problem, payment_id=payment_id)
            sub = check.subscription
            payment = await repo.payment_with_plan(conn, payment_id)
            if not await repo.cancel(conn, sub["id"], payment["user_id"]):
                return SubResult("already_ended", payment_id=payment_id)
            await repo.audit(
                conn,
                admin_id,
                "sub_revoke",
                user_id=payment["user_id"],
                subscription_id=sub["id"],
                payment_id=payment_id,
                reason=text,
                detail=f"{payment['reference']} -> {sub['plan_name']} ({sub['phase']}): {text}",
            )
        log.info("payment approval revoked by an admin")
        return SubResult(
            "ok",
            payment["user_id"],
            sub["id"],
            payment_id,
            plan_name=payment["plan_name"],
            phase=sub["phase"],
            reference=payment["reference"],
            starts_at=sub["started_at"],
        )

    # ------------------------------------------------------------ shorten (S3)

    async def _shorten_check(
        self, db, sub_id: int, days: int, *, payment=None, lock: bool, expected: str | None = None
    ) -> ShortenCheck:
        """The one place that decides a shortening. With lock=True it runs inside the transaction
        that will apply it: the account and every running / queued subscription are row-locked
        first, so what is checked is exactly what is changed."""
        sub = await repo.get_subscription(db, sub_id)
        if sub is None:
            return ShortenCheck("not_found")
        if lock:
            if await repo.lock_user(db, sub["user_id"]) is None:
                return ShortenCheck("not_found")
            live = await repo.lock_live(db, sub["user_id"])
            sub = await repo.get_subscription(db, sub_id)  # re-read under the lock
        else:
            live = await repo.lock_live(db, sub["user_id"], lock=False)
        old_end = sub["current_period_end"]
        seen = old_end.isoformat() if old_end is not None else ""
        if expected is not None and expected != seen:
            return ShortenCheck("changed", sub, payment, days, old_end)
        if sub["phase"] == "ended":
            return ShortenCheck("not_active", sub, payment, days, old_end)
        if old_end is None:
            return ShortenCheck("no_end", sub, payment, days, old_end)
        if not 1 <= days <= MAX_SHORTEN_DAYS:
            return ShortenCheck("bad_days", sub, payment, days, old_end, expected=seen)
        now = await repo.db_now(db)
        floor = max(now, sub["started_at"]) + MIN_LEFT
        max_days = max(0, (old_end - floor) // timedelta(days=1))
        new_end = await repo.minus_days(db, old_end, days)
        if new_end < floor:
            return ShortenCheck(
                "too_much", sub, payment, days, old_end, new_end, max_days, expected=seen
            )
        shifts: list[Shift] = []
        for other in live:
            if other["id"] == sub_id:
                continue
            ends = other["current_period_end"]
            if other["started_at"] >= old_end:  # queued behind it: moves with it
                shifts.append(
                    Shift(
                        other["id"],
                        other["plan_name"],
                        other["started_at"],
                        ends,
                        await repo.minus_days(db, other["started_at"], days),
                        await repo.minus_days(db, ends, days) if ends is not None else None,
                    )
                )
            elif ends is None or ends > sub["started_at"]:
                # overlaps this one: not a state the policy creates, so do not guess
                return ShortenCheck("overlap", sub, payment, days, old_end, new_end, expected=seen)
        return ShortenCheck(
            None, sub, payment, days, old_end, new_end, max_days, tuple(shifts), expected=seen
        )

    async def preview_shorten(self, admin_id: int, sub_id: int, days: int) -> ShortenCheck | None:
        """For the confirmation page. None for a non-admin."""
        if not self.is_admin(admin_id):
            return None
        return await self._shorten_check(self._pool, sub_id, days, lock=False)

    async def shorten(
        self,
        admin_id: int,
        sub_id: int,
        days: int,
        reason: str | None,
        *,
        confirmed: bool,
        expected: str,
    ) -> SubResult:
        """Take `days` off a running or queued subscription; what is queued behind it moves too."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        refusal, text = self._checks(confirmed, reason)
        if refusal is not None:
            return refusal
        return await self._apply_shorten(admin_id, sub_id, days, text, expected, payment_id=None)

    async def _apply_shorten(
        self, admin_id, sub_id, days, text, expected, *, payment_id: int | None
    ) -> SubResult:
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                payment = None
                if payment_id is not None:
                    ext = await self._extension_check(conn, payment_id, lock=True)
                    if ext.problem is not None:
                        return SubResult(ext.problem, payment_id=payment_id)
                    payment = ext.payment
                    if ext.sub["id"] != sub_id or ext.days != days:
                        return SubResult("changed", payment_id=payment_id)
                check = await self._shorten_check(
                    conn, sub_id, days, payment=payment, lock=True, expected=expected
                )
                if check.problem is not None:
                    return SubResult(
                        check.problem,
                        check.sub["user_id"] if check.sub else None,
                        sub_id,
                        payment_id=payment_id,
                        max_days=check.max_days or None,
                    )
                sub = check.sub
                new_end = await repo.set_end_minus(conn, sub_id, days)
                if check.shifts:
                    await repo.shift_back(conn, [s.id for s in check.shifts], days)
                await repo.audit(
                    conn,
                    admin_id,
                    "sub_shorten",
                    user_id=sub["user_id"],
                    subscription_id=sub_id,
                    payment_id=payment_id,
                    reason=text,
                    detail=f"-{days}d {sub['plan_name']} ({sub['phase']}) "
                    f"{_day(check.old_end)} -> {_day(new_end)}, {len(check.shifts)} queued moved: "
                    f"{text}",
                )
        except asyncpg.UniqueViolationError:
            return SubResult("already_removed", payment_id=payment_id)  # raced a second removal
        log.info("subscription shortened by an admin")
        return SubResult(
            "ok",
            sub["user_id"],
            sub_id,
            payment_id,
            plan_name=sub["plan_name"],
            phase=sub["phase"],
            starts_at=sub["started_at"],
            ends_at=new_end,
            days=days,
            shifted=len(check.shifts),
        )

    # ------------------------------------------------------------ remove one payment's extension

    @staticmethod
    async def _extension_check(db, payment_id: int, *, lock: bool) -> ShortenCheck:
        """A payment's extension is removed only when it is PROVABLE what it added: an approved
        payment that EXTENDED an existing subscription added exactly its own `duration_days`.
        The payment that CREATED the subscription is refused (its share is not recorded: it may
        include converted upgrade time), and so is anything already removed. Never a guess."""
        payment = await repo.payment_with_plan(db, payment_id)
        if payment is None:
            return ShortenCheck("not_found")
        if payment["status"] != "approved":
            return ShortenCheck("not_approved", payment=payment)
        removed = await repo.removal_audit(db, payment_id)
        if removed is not None:
            return ShortenCheck("already_removed", payment=payment, removed_by=removed)
        if payment["subscription_id"] is None:
            return ShortenCheck("no_subscription", payment=payment)
        sub = await repo.get_subscription(db, payment["subscription_id"])
        if sub is None or sub["user_id"] != payment["user_id"]:
            return ShortenCheck("no_subscription", payment=payment)
        if sub["provider_ref"] == payment["reference"]:
            return ShortenCheck("ambiguous", sub, payment)  # it created the subscription
        if sub["phase"] == "ended":
            return ShortenCheck("already_ended", sub, payment)
        days = await db.fetchval("SELECT duration_days FROM payments WHERE id = $1", payment_id)
        return ShortenCheck(None, sub, payment, days)

    async def preview_remove_extension(self, admin_id: int, payment_id: int) -> ShortenCheck | None:
        if not self.is_admin(admin_id):
            return None
        ext = await self._extension_check(self._pool, payment_id, lock=False)
        if ext.problem is not None:
            return ext
        full = await self._shorten_check(
            self._pool, ext.sub["id"], ext.days, payment=ext.payment, lock=False
        )
        return full

    async def remove_extension(
        self,
        admin_id: int,
        payment_id: int,
        reason: str | None,
        *,
        confirmed: bool,
        expected: str,
    ) -> SubResult:
        """Take exactly the days one approved payment ADDED to its subscription off again. The
        payment row is never touched; the removal is audited against the payment, once."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        refusal, text = self._checks(confirmed, reason)
        if refusal is not None:
            return refusal
        ext = await self._extension_check(self._pool, payment_id, lock=False)
        if ext.problem is not None:
            return SubResult(ext.problem, payment_id=payment_id)
        return await self._apply_shorten(
            admin_id, ext.sub["id"], ext.days, text, expected, payment_id=payment_id
        )


def _day(value) -> str:
    return value.strftime("%Y-%m-%d") if value is not None else "no end"
