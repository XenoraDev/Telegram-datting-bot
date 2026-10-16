"""Manual UPI payments (P6): the user pays outside the bot, an ADMIN verifies and approves.

    user:  start(plan) -> attach_evidence(screenshot) -> submit(utr?)         [created -> pending]
    admin: approve(payment) | reject(payment, reason)                         [pending -> final]

A screenshot is evidence only. Nothing a user sends can activate premium: the only code path that
creates / extends a subscription is `approve`, which needs an admin id from ADMIN_IDS, runs in one
transaction with the payment row locked, and re-checks that the payment is still pending. The P4
tables are reused as they are (`subscriptions` -> `EntitlementService` reads them).
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from base64 import b32encode
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncpg

from ..db import repo_payments as repo
from ..tiers import TIER_NAMES
from .admin import Page
from .evidence import Analysis, Expected, analyze_text, failed_analysis, utr_check
from .image_check import ImageError, ImageInfo, inspect
from .ocr import OcrEngine, OcrError
from .rate_limit import RateLimiter
from .upi import format_inr, parse_rupees, upi_uri

log = logging.getLogger(__name__)

PAGE_SIZE = 25
STALE_ORDER_HOURS = 24  # an order nobody submitted is cancelled after this long
EVIDENCE_RETENTION_DAYS = 90  # screenshots of finished payments are deleted after this long
STATUSES = ("pending", "approved", "rejected", "cancelled")
UPI_RE = re.compile(r"^[A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9.-]{1,31}$")
UTR_RE = re.compile(r"^[A-Z0-9]{6,35}$")
PLAN_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAX_PLAN_PRICE_MINOR = 5_000_000  # Rs 50,000: a sanity cap for a subscription
MIN_REASON = 3
MAX_REASON = 300


def clean_text(raw: str | None, limit: int, *, multiline: bool = False) -> str:
    """Printable characters only, whitespace squeezed, bounded."""
    text = "".join(c for c in (raw or "") if c.isprintable() or (multiline and c == "\n"))
    if multiline:
        text = "\n".join(" ".join(line.split()) for line in text.splitlines()).strip()
    else:
        text = " ".join(text.split())
    return text[:limit]


_CAPTION_PREFIX = re.compile(
    r"^\s*(?:utr|upi\s*ref(?:erence)?|ref(?:erence)?|txn|transaction)(?:\s*(?:no|id|number))?"
    r"(?:\s*[:#.\-]\s*|\s+)",  # a separator is required: "TXN123456" is itself an id
    re.IGNORECASE,
)


def _caption_utr(raw: str | None) -> str | None:
    """A photo caption such as "UTR: 4123 4567 8901": drop a leading label before validating."""
    return _CAPTION_PREFIX.sub("", raw or "", count=1)


def normalize_utr(raw: str | None) -> str | None:
    """None when no UTR was given; the upper-cased UTR; raises ValueError when malformed."""
    text = (raw or "").strip()
    if not text:
        return None
    text = text.replace(" ", "").replace("-", "").upper()
    if not UTR_RE.fullmatch(text):
        raise ValueError("invalid utr")
    return text


@dataclass(frozen=True, slots=True)
class Order:
    payment_id: int
    reference: str
    plan_name: str
    amount_minor: int
    duration_days: int
    upi_id: str
    payee_name: str
    instructions: str

    @property
    def amount(self) -> str:
        return format_inr(self.amount_minor)

    @property
    def uri(self) -> str:
        return upi_uri(self.upi_id, self.payee_name, self.amount_minor, self.reference)


@dataclass(frozen=True, slots=True)
class StartResult:
    # ok | unavailable | plan_unavailable | not_active | has_pending | rate_limited | busy
    kind: str
    order: Order | None = None
    payment: asyncpg.Record | None = None  # the pending one, for has_pending


@dataclass(frozen=True, slots=True)
class Overview:
    plans: list[asyncpg.Record]
    ready: bool  # payments configured and switched on
    premium_until: datetime | None
    open_payment: asyncpg.Record | None
    last_rejected: asyncpg.Record | None


@dataclass(frozen=True, slots=True)
class LiveSub:
    id: int
    plan_id: int
    level: int  # the plan's tier level (plan_badge); 0 = custom / untiered
    ends: datetime
    starts: datetime | None = None


@dataclass(frozen=True, slots=True)
class Purchase:
    """How an approved purchase is applied to what the user already has."""

    mode: str  # started | extended | upgraded | queued
    extend_id: int | None = None  # extended: the subscription whose end moves
    shift_ids: tuple[int, ...] = ()  # extended: queued ones behind it, pushed back by the same days
    end_ids: tuple[int, ...] = ()  # upgraded: subscriptions that stop now
    start: datetime | None = None  # queued: when the new subscription begins
    end_base: datetime | None = None  # the new subscription ends this + the plan's days


def decide_purchase(live: list[LiveSub], plan_id: int, level: int) -> Purchase:
    """The repurchase policy: deterministic, never two overlapping subscriptions of one tier.

    1. Same plan, or another plan of the SAME tier, still running or queued -> extend it (the days
       are added after its current end, not from today: nothing is lost). Anything queued behind
       it is pushed back by the same days, so the chain stays back-to-back with no overlap.
    2. A HIGHER tier than everything the user has -> upgrade now: the old subscriptions stop and
       their remaining time is carried over, so the new one ends at (old end + new days).
    3. A LOWER tier than something the user has -> queued: it starts when the current time ends.
    4. Nothing running -> a fresh subscription from now (an expired one is simply not live).
    """
    if not live:
        return Purchase("started")
    same = [s for s in live if s.plan_id == plan_id] or [s for s in live if s.level == level]
    if same:
        target = max(same, key=lambda s: s.ends)
        behind = tuple(
            s.id for s in live if s.id != target.id and s.starts and s.starts >= target.ends
        )
        return Purchase("extended", extend_id=target.id, shift_ids=behind)
    last_end = max(s.ends for s in live)
    if level > max(s.level for s in live):
        return Purchase("upgraded", end_ids=tuple(s.id for s in live), end_base=last_end)
    return Purchase("queued", start=last_end, end_base=last_end)


@dataclass(frozen=True, slots=True)
class PaymentResult:
    # ok | denied | rate_limited | not_found | already_handled | reason_required | no_evidence |
    # user_inactive | plan_inactive | duplicate_utr | invalid_state | invalid_utr | not_image |
    # too_big | corrupt
    kind: str
    payment_id: int | None = None
    user_id: int | None = None
    reference: str | None = None
    period_end: datetime | None = None
    reason: str | None = None
    mode: str | None = None  # approve: started | extended | upgraded | queued
    starts_at: datetime | None = None  # approve: when the subscription begins (queued: later)


@dataclass(frozen=True, slots=True)
class SaveResult:
    kind: str  # ok | denied | invalid | rate_limited | not_found | exists
    field: str | None = None


def _new_reference() -> str:
    return "PAY-" + b32encode(secrets.token_bytes(6)).decode()[:10]


def _usable(s: asyncpg.Record) -> bool:
    return bool(s["is_active"] and s["upi_id"] and s["payee_name"])


class PaymentService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        limiter: RateLimiter,
        admin_ids: frozenset[int] = frozenset(),
        ocr: OcrEngine | None = None,
    ) -> None:
        self._pool = pool
        self._limiter = limiter
        self._admin_ids = admin_ids
        self._ocr = ocr

    def is_admin(self, uid: int) -> bool:
        return uid in self._admin_ids

    # ============================================================ user side

    async def overview(self, uid: int) -> Overview:
        settings = await repo.get_settings(self._pool)
        last = await repo.latest_for_user(self._pool, uid)
        return Overview(
            plans=await repo.purchasable_plans(self._pool),
            ready=_usable(settings),
            premium_until=await repo.premium_until(self._pool, uid),
            open_payment=await repo.open_payment(self._pool, uid),
            last_rejected=last if last is not None and last["status"] == "rejected" else None,
        )

    async def start(self, uid: int, plan_id: int) -> StartResult:
        """Open (or re-show) the payment order for a plan. At most one open order per user."""
        if not self._limiter.allow("payment_start", uid):
            return StartResult("rate_limited")
        settings = await repo.get_settings(self._pool)
        if not _usable(settings):
            return StartResult("unavailable")
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                if await repo.user_status_shared(conn, uid) != "active":
                    return StartResult("not_active")
                plan = await repo.get_plan(conn, plan_id, lock=True)
                if (
                    plan is None
                    or not plan["is_active"]
                    or plan["price_minor"] is None
                    or plan["currency"] != "INR"
                ):
                    return StartResult("plan_unavailable")
                existing = await repo.open_payment(conn, uid, lock=True)
                if existing is not None and existing["status"] == "pending":
                    return StartResult("has_pending", payment=existing)
                if (
                    existing is not None
                    and existing["plan_id"] == plan["id"]
                    and existing["amount_minor"] == plan["price_minor"]
                    and existing["duration_days"] == plan["duration_days"]
                    and existing["pay_to"] == settings["upi_id"]
                ):
                    return StartResult("ok", _order(existing, settings))
                await repo.cancel_created(conn, uid)
                pid = await repo.insert_payment(
                    conn, _new_reference(), uid, plan, settings["upi_id"]
                )
                created = await repo.get_payment(conn, pid)
        except asyncpg.UniqueViolationError:
            return StartResult("busy")  # a concurrent start won the race: just try again
        return StartResult("ok", _order(created, settings))

    async def order(self, uid: int, payment_id: int) -> asyncpg.Record | None:
        """One of the user's own payments (never anyone else's)."""
        payment = await repo.get_payment(self._pool, payment_id)
        return payment if payment is not None and payment["user_id"] == uid else None

    async def attach_evidence(self, uid: int, payment_id: int, data: bytes) -> PaymentResult:
        """Validate and store the screenshot of an order that has not been submitted yet."""
        if not self._limiter.allow("payment_upload", uid):
            return PaymentResult("rate_limited")
        try:
            info = inspect(data)
        except ImageError as exc:
            return PaymentResult(exc.code)
        async with self._pool.acquire() as conn, conn.transaction():
            payment = await repo.get_payment(conn, payment_id, lock=True)
            if payment is None or payment["user_id"] != uid or payment["status"] != "created":
                return PaymentResult("invalid_state")
            sha = await repo.put_evidence(conn, payment_id, info.content_type, data)
            settings = await repo.get_settings(conn)
        await self._analyse_and_store(payment, settings["payee_name"], sha, data, info)
        return PaymentResult("ok", payment_id, uid, payment["reference"])

    async def _analyse_and_store(
        self,
        payment: asyncpg.Record,
        payee_name: str | None,
        sha: str,
        data: bytes,
        info: ImageInfo,
    ) -> None:
        """Advisory analysis of the screenshot. Never raises and never blocks the order: any
        trouble just means "no analysis" for the admin. The OCR text lives only in this frame;
        what is stored are the extracted fields and finding codes."""
        try:
            analysis = await self._read_screenshot(payment, payee_name, data, info)
            await repo.put_analysis(self._pool, payment["id"], sha, analysis)
            log.info("payment %s screenshot analysed: %s", payment["id"], analysis.verdict)
        except Exception:
            log.exception("payment %s screenshot analysis failed", payment["id"])

    async def _read_screenshot(
        self, payment: asyncpg.Record, payee_name: str | None, data: bytes, info: ImageInfo
    ) -> Analysis:
        if self._ocr is None:
            return failed_analysis("none", "ocr_unavailable")
        try:
            text = await self._ocr.read(data, info.width, info.height)
        except OcrError as exc:
            return failed_analysis(self._ocr.name, f"ocr_{exc.reason}")
        expected = Expected(
            amount_minor=payment["amount_minor"],
            upi_id=payment["pay_to"],
            payee_name=payee_name,
            created_at=payment["created_at"],
            now=datetime.now(UTC),
        )
        return analyze_text(text, expected, self._ocr.name)

    async def submit_screenshot(
        self, uid: int, data: bytes, utr_raw: str | None = None
    ) -> PaymentResult:
        """A photo from `uid`: the evidence of THEIR open order, resolved here from the user id
        (nothing the client sends picks the payment). One transaction with the order row locked:
        validate -> store -> created -> pending, so a second photo sent at the same moment finds
        the order already submitted. Result kinds: ok | no_order | already_submitted | not_image |
        too_big | corrupt | rate_limited | duplicate_utr. A malformed UTR is not an error: it is
        left out (`utr_ignored`) and the user may send it again as a message."""
        if not self._limiter.allow("payment_upload", uid):
            return PaymentResult("rate_limited")
        try:
            info = inspect(data)
        except ImageError as exc:
            return PaymentResult(exc.code)
        try:
            utr = normalize_utr(_caption_utr(utr_raw))
        except ValueError:
            utr = None
        ignored = bool(utr_raw and utr_raw.strip() and utr is None)
        async with self._pool.acquire() as conn, conn.transaction():
            payment = await repo.open_payment(conn, uid, lock=True)
            if payment is None:
                return PaymentResult("no_order")
            if payment["status"] != "created":
                return PaymentResult("already_submitted", payment["id"], uid, payment["reference"])
            if utr is not None and await repo.utr_in_use(conn, utr, payment["id"]):
                return PaymentResult("duplicate_utr")
            sha = await repo.put_evidence(conn, payment["id"], info.content_type, data)
            if not await repo.submit(conn, payment["id"], utr):
                return PaymentResult("already_submitted", payment["id"], uid, payment["reference"])
            settings = await repo.get_settings(conn)
        await self._analyse_and_store(payment, settings["payee_name"], sha, data, info)
        return PaymentResult(
            "ok",
            payment["id"],
            uid,
            payment["reference"],
            reason="utr_ignored" if ignored else None,
        )

    async def add_utr(self, uid: int, utr_raw: str) -> PaymentResult:
        """The UTR of the user's order that is already waiting for review. Server-resolved like
        submit_screenshot. Kinds: ok | no_order | invalid_state | utr_already | invalid_utr |
        duplicate_utr | rate_limited."""
        if not self._limiter.allow("payment_submit", uid):
            return PaymentResult("rate_limited")
        try:
            utr = normalize_utr(_caption_utr(utr_raw))
        except ValueError:
            return PaymentResult("invalid_utr")
        if utr is None:
            return PaymentResult("invalid_utr")
        async with self._pool.acquire() as conn, conn.transaction():
            payment = await repo.open_payment(conn, uid, lock=True)
            if payment is None:
                return PaymentResult("no_order")
            if payment["status"] != "pending":
                return PaymentResult("invalid_state")
            if payment["utr"] is not None:
                return PaymentResult("utr_already")
            if await repo.utr_in_use(conn, utr, payment["id"]):
                return PaymentResult("duplicate_utr")
            if not await repo.set_utr_if_missing(conn, payment["id"], utr):
                return PaymentResult("invalid_state")
        return PaymentResult("ok", payment["id"], uid, payment["reference"])

    async def submit(self, uid: int, payment_id: int, utr_raw: str | None) -> PaymentResult:
        """created -> pending. Needs a stored screenshot; the UTR is optional but checked."""
        if not self._limiter.allow("payment_submit", uid):
            return PaymentResult("rate_limited")
        try:
            utr = normalize_utr(utr_raw)
        except ValueError:
            return PaymentResult("invalid_utr")
        async with self._pool.acquire() as conn, conn.transaction():
            payment = await repo.get_payment(conn, payment_id, lock=True)
            if payment is None or payment["user_id"] != uid or payment["status"] != "created":
                return PaymentResult("invalid_state")
            if not await repo.has_evidence(conn, payment_id):
                return PaymentResult("no_evidence")
            if utr is not None and await repo.utr_in_use(conn, utr, payment_id):
                return PaymentResult("duplicate_utr")
            if not await repo.submit(conn, payment_id, utr):
                return PaymentResult("invalid_state")
        return PaymentResult("ok", payment_id, uid, payment["reference"])

    async def cancel(self, uid: int, payment_id: int) -> PaymentResult:
        async with self._pool.acquire() as conn, conn.transaction():
            payment = await repo.get_payment(conn, payment_id, lock=True)
            if payment is None or payment["user_id"] != uid:
                return PaymentResult("not_found")
            if not await repo.set_status(conn, payment_id, "created", "cancelled"):
                return PaymentResult("invalid_state")  # already submitted: only an admin decides
        return PaymentResult("ok", payment_id, uid, payment["reference"])

    # ============================================================ admin side

    def _gate(self, admin_id: int) -> PaymentResult | None:
        if not self.is_admin(admin_id):
            return PaymentResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return PaymentResult("rate_limited")
        return None

    async def list_payments(self, admin_id: int, *, status: str | None, page: int) -> Page:
        if not self.is_admin(admin_id):
            return Page([], 0, 0, 1)
        status = status if status in STATUSES else None
        total = await repo.count_payments(self._pool, status=status)
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        rows = await repo.list_payments(
            self._pool, status=status, limit=PAGE_SIZE, offset=page * PAGE_SIZE
        )
        return Page(rows, total, page, pages)

    async def counts(self, admin_id: int) -> asyncpg.Record | None:
        return await repo.payment_counts(self._pool) if self.is_admin(admin_id) else None

    async def detail(self, admin_id: int, payment_id: int) -> dict | None:
        """Everything a reviewer needs and nothing more: no profile, no chat, no contact data."""
        if not self.is_admin(admin_id):
            return None
        payment = await repo.get_payment(self._pool, payment_id)
        if payment is None or payment["status"] == "created":
            return None
        return {
            "payment": payment,
            "user_status": await repo.user_status_plain(self._pool, payment["user_id"]),
            "has_evidence": await repo.has_evidence(self._pool, payment_id),
            "same_images": await repo.same_image_elsewhere(self._pool, payment_id),
            "same_utr": await repo.same_utr_elsewhere(self._pool, payment_id),
            "analysis": await self._analysis_view(payment),
        }

    async def _analysis_view(self, payment: asyncpg.Record) -> dict | None:
        """The advisory screenshot analysis for the admin page (None = not analysed)."""
        row = await repo.get_analysis(self._pool, payment["id"])
        if row is None:
            return None
        return {
            "verdict": row["verdict"],
            "engine": row["engine"],
            "amount_minor": row["amount_minor"],
            "expected_minor": payment["amount_minor"],
            "payee": row["payee"],
            "expected_payee": payment["pay_to"],
            "utr": row["utr"],
            "utr_check": utr_check(row["utr"], payment["utr"]),
            "paid_at": row["paid_at"],
            "success_marker": row["success_marker"],
            "findings": json.loads(row["findings"]),
            "analyzed_at": row["analyzed_at"],
        }

    async def evidence(self, admin_id: int, payment_id: int) -> tuple[str, bytes] | None:
        if not self.is_admin(admin_id):
            return None
        row = await repo.get_evidence(self._pool, payment_id)
        return (row["content_type"], bytes(row["data"])) if row else None

    async def approve(self, admin_id: int, payment_id: int) -> PaymentResult:
        """pending -> approved AND the subscription in one transaction; safe to call twice."""
        if (refused := self._gate(admin_id)) is not None:
            return refused
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                payment = await repo.get_payment(conn, payment_id, lock=True)
                if payment is None:
                    return PaymentResult("not_found")
                if payment["status"] != "pending":
                    return PaymentResult(
                        "already_handled", payment_id, reference=payment["reference"]
                    )
                if await repo.user_status_shared(conn, payment["user_id"]) != "active":
                    return PaymentResult("user_inactive", payment_id)
                plan = await repo.get_plan(conn, payment["plan_id"], lock=True)
                if plan is None or not plan["is_active"]:
                    return PaymentResult("plan_inactive", payment_id)
                if not await repo.has_evidence(conn, payment_id):
                    return PaymentResult("no_evidence", payment_id)
                level = await repo.plan_level(conn, payment["plan_id"])
                rows = await repo.live_subscriptions_for_update(conn, payment["user_id"])
                live = [
                    LiveSub(
                        r["id"], r["plan_id"], r["level"], r["current_period_end"], r["started_at"]
                    )
                    for r in rows
                ]
                purchase = decide_purchase(live, payment["plan_id"], level)
                starts_at = await repo.db_now(conn)
                if purchase.mode == "extended":
                    sub_id = purchase.extend_id
                    period_end = await repo.extend_subscription(
                        conn, sub_id, payment["duration_days"]
                    )
                    if purchase.shift_ids:
                        await repo.shift_subscriptions(
                            conn, list(purchase.shift_ids), payment["duration_days"]
                        )
                    starts_at = next(r["started_at"] for r in rows if r["id"] == sub_id)
                else:
                    if purchase.end_ids:
                        await repo.end_subscriptions_now(conn, list(purchase.end_ids))
                    sub = await repo.new_subscription(
                        conn,
                        payment["user_id"],
                        payment["plan_id"],
                        payment["duration_days"],
                        payment["reference"],
                        purchase.start,
                        purchase.end_base,
                    )
                    sub_id, period_end, starts_at = (
                        sub["id"],
                        sub["current_period_end"],
                        sub["started_at"],
                    )
                if not await repo.approve(conn, payment_id, admin_id, sub_id):
                    raise _Rollback  # lost a race after all: undo the subscription too
                await repo.audit(
                    conn,
                    admin_id,
                    "payment_approve",
                    target=payment["user_id"],
                    detail=f"{payment['reference']} Rs {format_inr(payment['amount_minor'])} "
                    f"{plan['code']} {payment['duration_days']}d {purchase.mode}",
                )
        except _Rollback:
            return PaymentResult("already_handled", payment_id)
        except asyncpg.UniqueViolationError:
            return PaymentResult("duplicate_utr", payment_id)  # UTR already backs another approval
        log.info("payment approved")
        return PaymentResult(
            "ok",
            payment_id,
            payment["user_id"],
            payment["reference"],
            period_end,
            mode=purchase.mode,
            starts_at=starts_at,
        )

    async def reject(self, admin_id: int, payment_id: int, reason: str | None) -> PaymentResult:
        if (refused := self._gate(admin_id)) is not None:
            return refused
        reason = clean_text(reason, MAX_REASON)
        if len(reason) < MIN_REASON:
            return PaymentResult("reason_required", payment_id)
        async with self._pool.acquire() as conn, conn.transaction():
            payment = await repo.get_payment(conn, payment_id, lock=True)
            if payment is None:
                return PaymentResult("not_found")
            if not await repo.reject(conn, payment_id, admin_id, reason):
                return PaymentResult("already_handled", payment_id, reference=payment["reference"])
            await repo.audit(
                conn,
                admin_id,
                "payment_reject",
                target=payment["user_id"],
                detail=f"{payment['reference']}: {reason}"[:200],
            )
        log.info("payment rejected")
        return PaymentResult(
            "ok", payment_id, payment["user_id"], payment["reference"], reason=reason
        )

    # ------------------------------------------------------------ configuration (admin)

    async def get_settings(self, admin_id: int) -> asyncpg.Record | None:
        return await repo.get_settings(self._pool) if self.is_admin(admin_id) else None

    async def save_settings(
        self,
        admin_id: int,
        *,
        upi_id: str,
        payee_name: str,
        instructions: str,
        active: bool,
    ) -> SaveResult:
        if not self.is_admin(admin_id):
            return SaveResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return SaveResult("rate_limited")
        upi = upi_id.strip()
        payee = clean_text(payee_name, 60)
        text = clean_text(instructions, 500, multiline=True)
        if upi and not UPI_RE.fullmatch(upi):
            return SaveResult("invalid", "upi_id")
        if active and not upi:
            return SaveResult("invalid", "upi_id")
        if active and not payee:
            return SaveResult("invalid", "payee_name")
        async with self._pool.acquire() as conn, conn.transaction():
            await repo.save_settings(
                conn, upi or None, payee or None, text or None, active, admin_id
            )
            await repo.audit(
                conn,
                admin_id,
                "payment_config",
                detail=f"payments {'on' if active else 'off'}; UPI id and payee saved",
            )
        return SaveResult("ok")

    async def plans(self, admin_id: int) -> list[asyncpg.Record]:
        return await repo.all_paid_plans(self._pool) if self.is_admin(admin_id) else []

    async def save_plan(
        self,
        admin_id: int,
        plan_id: int | None,
        *,
        code: str,
        name: str,
        days: str,
        price: str,
        active: bool,
        tier: str | None = None,
    ) -> SaveResult:
        """Create (plan_id None) or edit a paid plan. Existing orders keep the price they saw.

        `tier` (premium / pro / vip) sets the plan's entitlements to that tier's template; on
        create it defaults to premium, on edit None leaves the entitlements as they are."""
        if not self.is_admin(admin_id):
            return SaveResult("denied")
        if not self._limiter.allow("admin_action", admin_id):
            return SaveResult("rate_limited")
        name = clean_text(name, 40)
        if not name:
            return SaveResult("invalid", "name")
        if not (days.strip().isascii() and days.strip().isdecimal() and 1 <= int(days) <= 3660):
            return SaveResult("invalid", "days")
        minor = parse_rupees(price)
        if minor is None or minor > MAX_PLAN_PRICE_MINOR:
            return SaveResult("invalid", "price")
        duration = int(days)
        if tier is not None and tier not in TIER_NAMES:
            return SaveResult("invalid", "tier")
        if plan_id is None and tier is None:
            tier = "premium"
        try:
            async with self._pool.acquire() as conn, conn.transaction():
                if plan_id is None:
                    code = code.strip()
                    if not PLAN_CODE_RE.fullmatch(code) or code == "free":
                        return SaveResult("invalid", "code")
                    await repo.insert_plan(conn, code, name, duration, minor, active, tier)
                elif not await repo.update_plan(conn, plan_id, name, duration, minor, active):
                    return SaveResult("not_found")
                else:
                    current = await repo.get_plan(conn, plan_id)
                    code = current["code"]
                    if tier is not None and tier != current["tier"]:
                        await repo.apply_tier(conn, plan_id, tier)
                await repo.audit(
                    conn,
                    admin_id,
                    "plan_save",
                    detail=f"{code} {tier or 'tier kept'} {duration}d Rs {format_inr(minor)} "
                    f"{'active' if active else 'inactive'}",
                )
        except asyncpg.UniqueViolationError:
            return SaveResult("exists", "code")
        return SaveResult("ok")

    async def ensure_default_plans(self, admin_id: int | None = None) -> dict[str, str]:
        """Make sure a Premium, a Pro and a VIP plan exist with their tier's entitlements.

        Idempotent and conservative, one transaction under an advisory lock:
          * missing  -> created, inactive, WITHOUT price or length (prices are the operator's);
          * exists with this tier (or no tier and no entitlements) -> only missing feature keys are
            added; no value, name, price, length or active flag is ever changed;
          * exists as a customised plan (own entitlements, or another tier) -> left untouched.
        Plans, subscriptions and payments are never deleted or altered. Returns {code: action}
        with action created | configured | completed | unchanged | kept_custom.
        """
        if admin_id is not None:
            if not self.is_admin(admin_id):
                return {}
            if not self._limiter.allow("admin_action", admin_id):
                return {}
        done: dict[str, str] = {}
        async with self._pool.acquire() as conn, conn.transaction():
            await repo.lock_plan_defaults(conn)
            for tier in TIER_NAMES:
                code = tier
                plan = await repo.plan_by_code(conn, code)
                if plan is None:
                    await repo.insert_default_plan(conn, code, repo.TIER_PLAN_NAMES[tier], tier)
                    done[code] = "created"
                    continue
                count = await repo.entitlement_count(conn, plan["id"])
                if plan["tier"] is None and count == 0:
                    await repo.apply_tier(conn, plan["id"], tier)
                    done[code] = "configured"
                elif plan["tier"] == tier:
                    added = await repo.add_missing_entitlements(conn, plan["id"], tier)
                    done[code] = "completed" if added else "unchanged"
                else:
                    done[code] = "kept_custom"
            if admin_id is not None:
                await repo.audit(
                    conn,
                    admin_id,
                    "plan_save",
                    detail="default tiers: " + ", ".join(f"{c} {a}" for c, a in done.items()),
                )
        if any(a in ("created", "configured", "completed") for a in done.values()):
            log.info("default tier plans set up: %s", done)
        return done

    # ------------------------------------------------------------ housekeeping

    async def maintain(self) -> tuple[int, int, int]:
        """(stale orders cancelled, old screenshots deleted, subscriptions marked expired)."""
        return (
            await repo.stale_created(self._pool, STALE_ORDER_HOURS),
            await repo.purge_reviewed_evidence(self._pool, EVIDENCE_RETENTION_DAYS),
            await repo.expire_subscriptions(self._pool),
        )


class _Rollback(Exception):
    """Internal: abort the approval transaction."""


def _order(payment: asyncpg.Record, settings: asyncpg.Record) -> Order:
    return Order(
        payment_id=payment["id"],
        reference=payment["reference"],
        plan_name=payment["plan_name"],
        amount_minor=payment["amount_minor"],
        duration_days=payment["duration_days"],
        upi_id=payment["pay_to"],
        payee_name=settings["payee_name"] or "",
        instructions=settings["instructions"] or "",
    )
