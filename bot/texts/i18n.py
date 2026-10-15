"""Central text lookup for translated (P4+) messages.

Existing P0-P3 messages stay in `texts/en.py` as plain constants. New user-facing strings are
registered per language here and looked up by key; a missing translation falls back to English,
and an unknown language code falls back to the default, so a lookup never fails.
"""

from __future__ import annotations

from . import en, hi

DEFAULT = "en"
LANGUAGES: dict[str, str] = {"en": "🇬🇧 English", "hi": "🇮🇳 हिन्दी"}  # code -> button label
SUPPORTED = tuple(LANGUAGES)

_CATALOGS: dict[str, dict[str, str]] = {"en": en.MESSAGES, "hi": hi.MESSAGES}


def normalize(code: str | None) -> str:
    return code if code in LANGUAGES else DEFAULT


def tr(key: str, lang: str | None = None, **fmt: object) -> str:
    """Translated string for `key` in `lang` (fallback: English). Raises KeyError if the key
    does not exist in English: that is a programming error and tests catch it."""
    text = _CATALOGS.get(normalize(lang), {}).get(key)
    if text is None:
        text = _CATALOGS[DEFAULT][key]
    return text.format(**fmt) if fmt else text
