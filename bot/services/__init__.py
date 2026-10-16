"""Business logic. Handlers call these; these call bot.db.* for SQL."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import asyncpg

from .account import AccountService
from .admin import AdminService
from .browse import BrowseService
from .chat_relay import ChatRelayService, ContextBuffer
from .entitlements import EntitlementService
from .language import LanguageService
from .matchmaking import MatchmakingService
from .moderation import ModerationService
from .onboarding import OnboardingService
from .payments import PaymentService
from .rate_limit import RateLimiter


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


def build_services(
    pool: asyncpg.Pool,
    wall_clock: Callable[[], float] = time.time,
    mono_clock: Callable[[], float] = time.monotonic,
    admin_ids: frozenset[int] = frozenset(),
) -> Services:
    """Create the service graph. Clocks are injectable so tests can control time."""
    limiter = RateLimiter(mono_clock)
    buffer = ContextBuffer(wall_clock)
    account = AccountService(pool, buffer)
    entitlements = EntitlementService(pool, wall_clock)
    return Services(
        limiter=limiter,
        buffer=buffer,
        account=account,
        onboarding=OnboardingService(pool, account),
        matchmaking=MatchmakingService(pool, limiter, entitlements),
        relay=ChatRelayService(pool, buffer, limiter, mono_clock),
        moderation=ModerationService(pool, buffer),
        browse=BrowseService(pool, limiter, entitlements),
        admin=AdminService(pool, buffer, limiter, admin_ids),
        entitlements=entitlements,
        language=LanguageService(pool),
        payments=PaymentService(pool, limiter, admin_ids),
    )
