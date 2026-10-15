"""Pure text rules shared by profile onboarding and the chat relay (no I/O)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# Telegram entity types that carry a link or contact detail.
FORBIDDEN_ENTITY_TYPES = frozenset(
    {"url", "text_link", "text_mention", "mention", "email", "phone_number"}
)

# Chat allows @usernames (the `mention` entity), but nothing that links to or hides a contact.
CHAT_FORBIDDEN_ENTITY_TYPES = FORBIDDEN_ENTITY_TYPES - {"mention"}

_MAIL_PROVIDERS = (
    r"(?:gmail|googlemail|yahoo|ymail|hotmail|outlook|live|icloud|protonmail|proton|rediffmail)"
)

_LINK_RE = re.compile(
    r":\s*/\s*/"  # "://" after ANY scheme (also catches homoglyph tricks like "hхtps://")
    r"|\bwww\s*\."  # www.
    r"|\bt\s*[.\s]\s*me\s*/"  # t.me/...
    r"|\bt\.me\b"  # t.me without a path
    rf"|\b{_MAIL_PROVIDERS}\s*\.\s*com\b"  # "gmail . com"
    r"|telegram\s*\.\s*(?:me|dog)"  # telegram.me / telegram.dog
    r"|\b[a-z0-9][a-z0-9-]{1,}\.(?:com|net|org|io|me|ru|in|co|xyz|app|link|ly|gl|cc|to|gg|vip|"
    r"shop|site|online|info|biz|us|uk|de|fr|tv|tk|ml|ga|cf)\b"  # bare domain
)
_HANDLE_RE = re.compile(
    r"@\s*[a-z0-9_]{3,}"  # @username
    r"|[a-z0-9._%+-]+\s*@\s*[a-z0-9-]+(?:\s*\.\s*[a-z0-9-]+)+"  # e-mail, even a@b.co
)
# A real e-mail address: the last label must be letters, so "meet @ 5.30" is not one.
_EMAIL_RE = re.compile(
    r"[a-z0-9._%+-]+\s*@\s*[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}"
    rf"|@\s*{_MAIL_PROVIDERS}\b"  # "john @ gmail . com"
)
_PHONE_RE = re.compile(r"(?:\d[\s().\-]*){8,}")  # profiles: 8+ digits in a row (strict)

# Light obfuscation: "john (at) gmail (dot) com", "evil[.]com", "john at gmail dot com".
_DEOBF = (
    (re.compile(r"\s*[(\[{<]\s*(?:at|@)\s*[)\]}>]\s*"), "@"),
    (re.compile(r"\s*[(\[{<]\s*(?:dot|\.)\s*[)\]}>]\s*"), "."),
    (re.compile(rf"\bat\s+(?={_MAIL_PROVIDERS}\b)"), "@"),
    (
        re.compile(
            r"\s+dot\s+(?=(?:com|net|org|io|xyz|app|link|ly|vip|shop|site|online|biz|me)\b)"
        ),
        ".",
    ),
    (re.compile(r"\b([a-z0-9._-]+)\s+at\s+([a-z0-9-]+)\s*\.\s*([a-z]{2,})\b"), r"\1@\2.\3"),
)

# --- phone numbers -----------------------------------------------------------------------------
_DIGIT_WORDS = {
    **{w: str(i) for i, w in enumerate(("zero", "one", "two", "three", "four", "five", "six"))},
    "seven": "7",
    "eight": "8",
    "nine": "9",
    # Hinglish (romanised Hindi) digit words
    "shunya": "0",
    "ek": "1",
    "do": "2",
    "teen": "3",
    "char": "4",
    "chaar": "4",
    "panch": "5",
    "paanch": "5",
    "chhe": "6",
    "chah": "6",
    "saat": "7",
    "aath": "8",
    "nau": "9",
}
_TOKEN_RE = re.compile(r"(?P<d>\d)|(?P<w>[^\W\d_]+)")
_MAX_GAP = 3  # at most this many separator characters between two digits of one number
_MAX_CARRY = 15
# Dates, times and grouped numbers are not phone numbers; they are masked before scanning.
_BENIGN_NUMBER_RE = re.compile(
    r"\b\d{1,2}[/.\-]\d{1,2}[/.\-](?:\d{4}|\d{2})\b"  # 12/05/2024
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b"  # 10:30
    r"|\b\d{1,3}(?:,\d{3})+\b"  # 1,000,000
    r"|\b\d{1,2}(?:,\d{2})+,\d{3}\b"  # 1,00,000
)
_CUE_RE = re.compile(
    r"\b(?:call|calls|calling|whats\s?app|watsapp|wapp|number|num|phone|ph|mobile|mob|cell|"
    r"contact|dial|ring|sms|text\s+me|signal|viber|imo)\b"
)

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


@dataclass(frozen=True, slots=True)
class PhoneCarry:
    """The tiny state kept between a user's consecutive chat messages to catch split numbers:
    at most the last 15 digits (no text) and whether a "call me / number" cue was just seen."""

    digits: str = ""
    cue: bool = False

    def __bool__(self) -> bool:
        return bool(self.digits) or self.cue


NO_CARRY = PhoneCarry()


def _ascii_digits(t: str) -> str:
    """Map Unicode decimal digits (Arabic-Indic, Devanagari, ...) to 0-9; drop combining marks."""
    out = []
    for c in t:
        cat = unicodedata.category(c)
        if cat in ("Mn", "Me"):
            continue  # e.g. the keycap combining marks of 9️⃣
        if cat == "Nd":
            c = str(unicodedata.decimal(c))
        out.append(c)
    return "".join(out)


def _is_phone_run(digits: str, *, plus: bool, formatted: bool, cue: bool) -> bool:
    """Context-aware decision for one run of digits (no blanket 'N digits' rule)."""
    n = len(digits)
    if n >= 11:
        return True  # 91xxxxxxxxxx, 0xxxxxxxxxx, E.164 ...
    if n == 10:
        # Indian mobiles start 6-9, NANP numbers 2-9; "1234567890"/"1000000000" need a cue.
        return digits[0] in "23456789" or plus or formatted or cue
    if n in (8, 9):
        return plus or cue
    if n == 7:
        return cue
    return False


def scan_phone(n: str, carry: PhoneCarry = NO_CARRY) -> tuple[bool, PhoneCarry]:
    """(is a phone number, carry for the next message) for NORMALISED text `n`."""
    t = _ascii_digits(_BENIGN_NUMBER_RE.sub("x", n))
    cue = bool(_CUE_RE.search(t))
    runs: list[list] = []  # [digits, start, end, formatted]
    prev_end = None
    for m in _TOKEN_RE.finditer(t):
        digit = m.group("d") or _DIGIT_WORDS.get(m.group("w"))
        if digit is None:
            prev_end = None
            continue
        if prev_end is not None and m.start() - prev_end <= _MAX_GAP and runs:
            run = runs[-1]
            run[0] += digit
            run[3] = run[3] or m.start() > prev_end
            run[2] = m.end()
        else:
            runs.append([digit, m.start(), m.end(), False])
        prev_end = m.end()

    effective_cue = cue or carry.cue
    found = False
    joined: list[str] = []
    for i, (digits, start, _end, formatted) in enumerate(runs):
        if i == 0 and carry.digits and not any(c.isalnum() for c in t[:start]):
            digits = carry.digits + digits  # a number started in an earlier message
        joined.append(digits)
        plus = "+" in t[max(0, start - _MAX_GAP) : start]
        if _is_phone_run(digits, plus=plus, formatted=formatted, cue=effective_cue):
            found = True
    if found:
        return True, NO_CARRY
    # Only a run that finishes the message can be continued by the next one.
    tail = joined[-1][-_MAX_CARRY:] if runs and len(t) - runs[-1][2] <= _MAX_GAP else ""
    return False, PhoneCarry(tail, cue)


def _deobfuscate(n: str) -> str:
    for pattern, repl in _DEOBF:
        n = pattern.sub(repl, n)
    return n


def _link_or_email(n: str) -> str | None:
    for candidate in (n, _deobfuscate(n)):
        if _LINK_RE.search(candidate):
            return "link"
        if _EMAIL_RE.search(candidate):
            return "contact"
    return None


def chat_contact_violation(
    text: str,
    entity_types: tuple[str, ...] | frozenset[str] = (),
    carry: PhoneCarry = NO_CARRY,
) -> tuple[str | None, PhoneCarry]:
    """Chat filter: 'link' | 'contact' | None plus the carry for this user's next message.

    @usernames are allowed; links, e-mails and phone numbers (also split over messages,
    spelled out or separated) are not."""
    if CHAT_FORBIDDEN_ENTITY_TYPES.intersection(entity_types):
        return "link", NO_CARRY
    n = normalize(text)
    found = _link_or_email(n)
    if found:
        return found, NO_CARRY
    is_phone, new_carry = scan_phone(n, carry)
    if is_phone:
        return "contact", NO_CARRY
    return None, new_carry


def contact_violation(text: str, entity_types: tuple[str, ...] | frozenset[str] = ()) -> str | None:
    """Strict check for profile text: also rejects @usernames and any run of 8+ digits."""
    if FORBIDDEN_ENTITY_TYPES.intersection(entity_types):
        return "link"
    n = normalize(text)
    found = _link_or_email(n)
    if found:
        return found
    if _HANDLE_RE.search(n) or _PHONE_RE.search(n) or scan_phone(n)[0]:
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
