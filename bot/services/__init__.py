"""Business logic. Handlers call these; these call bot.db.* for SQL."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import asyncpg

from ..loadtest.safety import LoadTestSettings
from .account import AccountService
from .admin import AdminService
from .boost import BoostService
from .browse import BrowseService
from .chat_relay import ChatRelayService, ContextBuffer
from .entitlements import EntitlementService
from .filters import FilterService
from .language import LanguageService
from .loadtest import LoadTestService
from .matchmaking import MatchmakingService
from .moderation import ModerationService
from .ocr import OcrEngine, TesseractOcr
from .onboarding import OnboardingService
from .partner_card import PartnerCardService
from .payments import PaymentService
from .privacy import PrivacyService
from .rate_limit import RateLimiter
from .tickets import TicketService
from .tier_admin import TierAdminService


@dataclass(frozen=True)
class Services:
    limiter: RateLimiter
    buffer: ContextBuffer
    account: AccountService
    onboarding: OnboardingService
    matchmaking: MatchmakingService
    relay: ChatRelayService
    moderation: ModerationService
    browse: BrowseService
    admin: AdminService
    entitlements: EntitlementService
    language: LanguageService
    payments: PaymentService
    cards: PartnerCardService
    privacy: PrivacyService
    filters: FilterService
    boost: BoostService
    tiers: TierAdminService
    loadtest: LoadTestService
    tickets: TicketService


def build_services(
    pool: asyncpg.Pool,
    wall_clock: Callable[[], float] = time.time,
    mono_clock: Callable[[], float] = time.monotonic,
    admin_ids: frozenset[int] = frozenset(),
    ocr: OcrEngine | None = None,
    loadtest_settings: LoadTestSettings | None = None,
) -> Services:
    """Create the service graph. Clocks are injectable so tests can control time."""
    limiter = RateLimiter(mono_clock)
    buffer = ContextBuffer(wall_clock)
    account = AccountService(pool, buffer)
    entitlements = EntitlementService(pool, wall_clock)
    filters = FilterService(pool, entitlements)
    return Services(
        limiter=limiter,
        buffer=buffer,
        account=account,
        onboarding=OnboardingService(pool, account),
        matchmaking=MatchmakingService(pool, limiter, entitlements),
        relay=ChatRelayService(pool, buffer, limiter, mono_clock),
        moderation=ModerationService(pool, buffer),
        browse=BrowseService(pool, limiter, entitlements, filters),
        admin=AdminService(pool, buffer, limiter, admin_ids),
        entitlements=entitlements,
        language=LanguageService(pool),
        payments=PaymentService(pool, limiter, admin_ids, ocr or TesseractOcr()),
        cards=PartnerCardService(pool, entitlements, limiter),
        privacy=PrivacyService(pool),
        filters=filters,
        boost=BoostService(pool, limiter, entitlements),
        tiers=TierAdminService(pool, limiter, admin_ids),
        loadtest=LoadTestService(pool, limiter, admin_ids, loadtest_settings),
        tickets=TicketService(pool, limiter, admin_ids),
    )
