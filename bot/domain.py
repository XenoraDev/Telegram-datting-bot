"""Shared constants and value sets (no I/O)."""

from __future__ import annotations

MIN_AGE = 18  # also enforced by a CHECK constraint on profiles.age
MAX_AGE = 80
NAME_MAX = 30
CITY_MAX = 40
BIO_MAX = 300
MESSAGE_MAX = 1000  # relayed chat text, characters

GENDERS = ("m", "f", "o")
LOOKING_FOR = ("m", "f", "any")
GENDER_LABEL = {"m": "Male", "f": "Female", "o": "Other"}
LOOKING_LABEL = {"m": "Men", "f": "Women", "any": "Everyone"}

# Country shown as a flag next to the city/region (name only, never a location).
COUNTRIES = {
    "IN": "India",
    "US": "USA",
    "GB": "UK",
    "PK": "Pakistan",
    "BD": "Bangladesh",
    "NP": "Nepal",
    "AE": "UAE",
    "CA": "Canada",
    "AU": "Australia",
    "ZZ": "Other",
}
GENDER_EMOJI = {"m": "👨", "f": "👩", "o": "🧑"}


def flag(country: str | None) -> str:
    """Flag emoji for an ISO alpha-2 code; a globe for unknown / 'other' / missing."""
    if not country or country == "ZZ" or country not in COUNTRIES:
        return "🌍"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country)


REPORT_REASONS = ("spam", "harassment", "sexual", "minor", "scam", "other")

# user statuses
ONBOARDING = "onboarding"
ACTIVE = "active"
BANNED = "banned"
BLOCKED_UNDERAGE = "blocked_underage"
DELETED = "deleted"

# report / abuse policy
REPORTS_PER_HOUR = 5
REPORTS_PER_DAY = 20
REPORT_WINDOW_DAYS = 7  # a chat can only be reported this long after it ended
RESTRICT_AFTER_DISTINCT_REPORTERS = 3  # within 24h -> temporary chat cooldown
COOLDOWN_HOURS = 24
ABUSE_MUTE_MINUTES = 5
REMATCH_BLOCK_MINUTES = 10  # don't re-pair two users who just talked
QUEUE_TIMEOUT_MINUTES = 30
CHAT_INACTIVITY_TIMEOUT_MINUTES = 10  # an active chat with no message for this long is closed
