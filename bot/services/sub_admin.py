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

import asyncpg

from ..db import repo_subscriptions as repo
from .payments import MIN_REASON, clean_text
from .rate_limit import RateLimiter

log = logging.getLogger(__name__)

MAX_REASON = 300


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


def _day(value) -> str:
    return value.strftime("%Y-%m-%d") if value is not None else "no end"
