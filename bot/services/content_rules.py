"""Pure text rules shared by profile onboarding and the chat relay (no I/O)."""

from __future__ import annotations

import re
import unicodedata

# Telegram entity types that carry a link or contact detail.
FORBIDDEN_ENTITY_TYPES = frozenset(
    {"url", "text_link", "text_mention", "mention", "email", "phone_number"}
)

_LINK_RE = re.compile(
    r":\s*/\s*/"  # "://" after ANY scheme (also catches homoglyph tricks like "hхtps://")
    r"|\bwww\s*\."  # www.
    r"|\bt\s*[.\s]\s*me\s*/"  # t.me/...
    r"|telegram\s*\.\s*(?:me|dog)"  # telegram.me / telegram.dog
    r"|\b[a-z0-9][a-z0-9-]{1,}\.(?:com|net|org|io|me|ru|in|co|xyz|app|link|ly|gl|cc|to|gg|vip|"
    r"shop|site|online|info|biz|us|uk|de|fr|tv|tk|ml|ga|cf)\b"  # bare domain
)
_HANDLE_RE = re.compile(
    r"@\s*[a-z0-9_]{3,}"  # @username
    r"|[a-z0-9._%+-]+\s*@\s*[a-z0-9-]+(?:\s*\.\s*[a-z0-9-]+)+"  # e-mail, even a@b.co
)
_PHONE_RE = re.compile(r"(?:\d[\s().\-]*){8,}")  # 8+ digits in a row (with separators)

_SEXUAL_RE = re.compile(
    r"\b(?:nudes?|sexting|sex\s*chat|escort|onlyfans|cam\s*show|porn\w*|"
    r"sugar\s*(?:daddy|baby|mommy|momma)|(?:send|dick|nude)\s*pics?)\b"
)
_MINOR_KW_RE = re.compile(
    r"\b(?:underage|under\s*age|under\s*18|under\s*eighteen|loli\w*|shota|pedo\w*|jailbait|"
    r"school\s*(?:girl|boy)s?)\b"
)
# Matches "I am 15", "im 16", "aged 17", "14 yo"; not "I'm 5'9" or "I am 10 minutes away".
_MINOR_CLAIM_RE = re.compile(
    r"\b(?:i\s*(?:am|['’]\s*m)|im|aged?)\s*:?\s*(?:only\s*)?(?:1[0-7]|[0-9])\b"
    r"(?!\s*(?:['’\"%:]|cm|ft|feet|kg|lbs?|km|mins?|minutes?|hours?|hrs?|st|nd|rd|th|m\b))"
    r"|\b(?:1[0-7]|[0-9])\s*(?:yo|y/o|y\.o\.|yrs?\s*old|years?\s*old)\b"
)


def normalize(text: str) -> str:
    """NFKC + casefold, with invisible/format characters (zero-width etc.) removed."""
    t = unicodedata.normalize("NFKC", text)
    t = "".join(c for c in t if unicodedata.category(c) != "Cf")
    return t.casefold()


def contact_violation(text: str, entity_types: tuple[str, ...] | frozenset[str] = ()) -> str | None:
    """'link' | 'contact' | None for text that would leak a link, handle, e-mail or phone."""
    if FORBIDDEN_ENTITY_TYPES.intersection(entity_types):
        return "link"
    n = normalize(text)
    if _LINK_RE.search(n):
        return "link"
    if _HANDLE_RE.search(n) or _PHONE_RE.search(n):
        return "contact"
    return None


def profile_text_violation(text: str) -> str | None:
    """'link' | 'contact' | 'minor' | 'sexual' | None for name / city / bio text."""
    found = contact_violation(text)
    if found:
        return found
    n = normalize(text)
    if _MINOR_KW_RE.search(n) or _MINOR_CLAIM_RE.search(n):
        return "minor"
    if _SEXUAL_RE.search(n):
        return "sexual"
    return None
