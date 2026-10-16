"""Test configuration: presets, hard safety limits and strict validation.

The limits are an operator decision (environment variables on the worker/web service), so the
admin UI can lower a test but never raise the ceiling. All parsing here is pure and side-effect
free.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

# name -> (concurrent chats, messages per chat per minute). Workloads only, NOT capacity claims.
PRESETS: dict[str, tuple[int, int]] = {
    "light": (50, 40),
    "medium": (100, 40),
    "heavy": (250, 50),
    "stress": (500, 50),
}
PROFILES = (*PRESETS, "custom")
# The manual ladder; the admin climbs it one explicit, separately confirmed step at a time.
LADDER = (50, 100, 250, 500, 1000)

DEFAULT_CUSTOM = (100, 50)
DEFAULT_DURATION_S = 300
DEFAULT_RAMP_S = 60

_INT_RE = re.compile(r"^[0-9]{1,9}$")


@dataclass(frozen=True, slots=True)
class Limits:
    """Ceilings the tester refuses to exceed. Overridable per deployment via LOAD_TEST_MAX_*."""

    max_chats: int = 2000
    max_msgs_per_chat_min: int = 120
    max_total_msgs_per_min: int = 100_000
    max_duration_s: int = 1800
    max_ramp_s: int = 900
    max_total_messages: int = 3_000_000
    min_duration_s: int = 10

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Limits:
        base = cls()
        values = {}
        for name in (
            "max_chats",
            "max_msgs_per_chat_min",
            "max_total_msgs_per_min",
            "max_duration_s",
            "max_ramp_s",
            "max_total_messages",
        ):
            raw = env.get(f"LOAD_TEST_{name.upper()}", "").strip()
            values[name] = (
                int(raw) if _INT_RE.fullmatch(raw) and int(raw) > 0 else getattr(base, name)
            )
        # Hard stop: synthetic ids are allocated in blocks of 10,000 per run.
        values["max_chats"] = min(values["max_chats"], HARD_MAX_CHATS)
        return cls(**values)


HARD_MAX_CHATS = 4000  # 2 synthetic users per chat must fit the per-run id block of 10,000


@dataclass(frozen=True, slots=True)
class TestConfig:
    profile: str
    chats: int
    msgs_per_chat_min: int
    duration_s: int
    ramp_s: int

    __test__ = False  # not a pytest class

    @property
    def total_msgs_per_min(self) -> int:
        return self.chats * self.msgs_per_chat_min

    @property
    def msgs_per_s(self) -> float:
        return self.total_msgs_per_min / 60.0

    @property
    def full_load_messages(self) -> int:
        """Messages sent while all chats are active (the steady-state part)."""
        return round(self.msgs_per_s * self.duration_s)

    @property
    def ramp_messages(self) -> int:
        """Chats join evenly over the ramp-up, so on average half of them are active."""
        return round(self.msgs_per_s * self.ramp_s / 2)

    @property
    def estimated_total_messages(self) -> int:
        return self.full_load_messages + self.ramp_messages

    @property
    def total_runtime_s(self) -> int:
        return self.ramp_s + self.duration_s

    def as_json(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "chats": self.chats,
            "msgs_per_chat_min": self.msgs_per_chat_min,
            "duration_s": self.duration_s,
            "ramp_s": self.ramp_s,
        }

    @property
    def digest(self) -> str:
        return config_hash(self.as_json())

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> TestConfig:
        return cls(
            profile=str(data["profile"]),
            chats=int(data["chats"]),  # type: ignore[call-overload]
            msgs_per_chat_min=int(data["msgs_per_chat_min"]),  # type: ignore[call-overload]
            duration_s=int(data["duration_s"]),  # type: ignore[call-overload]
            ramp_s=int(data["ramp_s"]),  # type: ignore[call-overload]
        )


def config_hash(config: Mapping[str, object]) -> str:
    """Canonical SHA-256 of a configuration: what a Telegram confirmation is bound to."""
    canonical = json.dumps(dict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def preset_config(name: str) -> TestConfig:
    chats, rate = PRESETS[name] if name in PRESETS else DEFAULT_CUSTOM
    return TestConfig(
        profile=name if name in PRESETS else "custom",
        chats=chats,
        msgs_per_chat_min=rate,
        duration_s=DEFAULT_DURATION_S,
        ramp_s=DEFAULT_RAMP_S,
    )


def _whole(raw: str | None, label: str, lo: int, hi: int, errors: dict[str, str]) -> int | None:
    text = (raw or "").strip().replace(",", "")
    if not _INT_RE.fullmatch(text):
        errors[label] = "Enter a whole number."
        return None
    value = int(text)
    if not lo <= value <= hi:
        errors[label] = f"Must be between {lo:,} and {hi:,}."
        return None
    return value


def parse_config(
    form: Mapping[str, str], limits: Limits
) -> tuple[TestConfig | None, dict[str, str]]:
    """Validate an admin form. Returns (config, {}) or (None, {field: message}).

    Everything is rejected, never clamped: zero / negative / fractional / malformed values, and
    anything above the limits (chats, rate per chat, total rate, duration, ramp-up, total
    messages). The profile is derived: a preset name only stays if the numbers still match it.
    """
    errors: dict[str, str] = {}
    chats = _whole(form.get("chats"), "chats", 1, limits.max_chats, errors)
    rate = _whole(
        form.get("msgs_per_chat_min"), "msgs_per_chat_min", 1, limits.max_msgs_per_chat_min, errors
    )
    if "duration_s" in form:  # API / tests may pass seconds directly
        duration = _whole(
            form.get("duration_s"),
            "duration_s",
            limits.min_duration_s,
            limits.max_duration_s,
            errors,
        )
    else:
        minutes = _whole(
            form.get("duration_min"), "duration_min", 1, limits.max_duration_s // 60, errors
        )
        duration = minutes * 60 if minutes is not None else None
    ramp = _whole(form.get("ramp_s"), "ramp_s", 0, limits.max_ramp_s, errors)
    if errors or chats is None or rate is None or duration is None or ramp is None:
        return None, errors

    if chats * rate > limits.max_total_msgs_per_min:
        errors["msgs_per_chat_min"] = (
            f"{chats:,} chats x {rate} msg/min = {chats * rate:,} msg/min is above the limit of "
            f"{limits.max_total_msgs_per_min:,} msg/min."
        )
    if ramp > duration:
        errors["ramp_s"] = "The ramp-up cannot be longer than the test duration."

    requested = (form.get("profile") or "custom").strip().lower()
    if requested not in PROFILES:
        errors["profile"] = "Unknown test profile."
        requested = "custom"
    profile = (
        requested if requested in PRESETS and PRESETS[requested] == (chats, rate) else "custom"
    )
    config = TestConfig(profile, chats, rate, duration, ramp)
    if not errors and config.estimated_total_messages > limits.max_total_messages:
        errors["duration_min"] = (
            f"About {config.estimated_total_messages:,} messages is above the limit of "
            f"{limits.max_total_messages:,}."
        )
    return (None, errors) if errors else (config, {})
