"""Business logic. Handlers call these; these call bot.db.* for SQL."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import asyncpg

from .account import AccountService
from .chat_relay import ChatRelayService, ContextBuffer
from .matchmaking import MatchmakingService
from .moderation import ModerationService
from .onboarding import OnboardingService
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


def build_services(
    pool: asyncpg.Pool,
    wall_clock: Callable[[], float] = time.time,
    mono_clock: Callable[[], float] = time.monotonic,
) -> Services:
    """Create the service graph. Clocks are injectable so tests can control time."""
    limiter = RateLimiter(mono_clock)
    buffer = ContextBuffer(wall_clock)
    account = AccountService(pool, buffer)
    return Services(
        limiter=limiter,
        buffer=buffer,
        account=account,
        onboarding=OnboardingService(pool, account),
        matchmaking=MatchmakingService(pool, limiter),
        relay=ChatRelayService(pool, buffer, limiter, mono_clock),
        moderation=ModerationService(pool, buffer),
    )
