"""Advisory analysis of a payment screenshot (P8). Pure functions: text in, findings out.

The screenshot is EVIDENCE for a human. Nothing here can approve, reject or change a payment: the
result is shown to the admin next to the picture, and the admin still decides. The text comes from
an OCR engine (see ocr.py); this module only looks for the few fields that matter, compares them
with the order, and returns codes (never the raw text, which is dropped by the caller).

    amount        the order's amount appears as a rupee amount (amount_mismatch / _not_found)
    payee         the payee UPI id (or name) is the order's (payee_mismatch / _unclear / _not_found)
    reference     a UTR / transaction id, compared later with the one the user typed
    date, time    printed as India time; must not predate the order or lie in the future
    status        "success" / "failed" / "pending" wording

Verdicts: VALIDATION_OK, VALIDATION_WARNING, VALIDATION_MISMATCH, VALIDATION_UNREADABLE.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher

OK = "VALIDATION_OK"
WARNING = "VALIDATION_WARNING"
MISMATCH = "VALIDATION_MISMATCH"
UNREADABLE = "VALIDATION_UNREADABLE"
VERDICTS = (OK, WARNING, MISMATCH, UNREADABLE)

IST = timedelta(hours=5, minutes=30)
ORDER_SLACK = timedelta(minutes=10)  # clocks differ a little
MIN_TEXT_CHARS = 12  # fewer letters / digits than this is not a readable screenshot

# findings that make the verdict a mismatch / a warning (anything else is informational)
MISMATCH_CODES = frozenset({"amount_mismatch", "payee_mismatch", "payment_failed"})
WARNING_CODES = frozenset(
    {
        "amount_not_found",
        "amount_unclear",
        "payee_not_found",
        "payee_unclear",
        "payment_pending",
        "time_too_old",
        "time_in_future",
        "success_not_shown",
    }
)

_UPI_RE = re.compile(r"(?<![A-Za-z0-9._-])([A-Za-z0-9._-]{2,64}@[A-Za-z][A-Za-z0-9.-]{1,31})")
_SYMBOL = r"(?:₹|Rs\.?|INR|¥|€|%|\?)"
_NUM = r"([0-9][0-9,]{0,10}(?:\.[0-9]{1,2})?)"
_MARKED_AMOUNT = re.compile(rf"{_SYMBOL}\s*{_NUM}", re.IGNORECASE)
_LABELLED_AMOUNT = re.compile(
    rf"(?:amount|paid|paying|sent|total|debited|transferred)\W{{0,12}}{_NUM}", re.IGNORECASE
)
_PLAIN_AMOUNT = re.compile(r"(?<![0-9.,])([0-9]{1,7}\.[0-9]{2})(?![0-9])")
_UTR_LABELLED = re.compile(
    r"(?:UTR|UPI\s*Ref(?:erence)?(?:\s*(?:No|ID|Number))?|Transaction\s*ID|Txn\s*ID|"
    r"Ref(?:erence)?\s*(?:No|ID|Number))\W{0,6}([A-Z0-9]{8,35})",
    re.IGNORECASE,
)
_UTR_BARE = re.compile(r"(?<![0-9])([0-9]{12})(?![0-9])")
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
_TIME = r"(?:[ ,at]{1,6})?(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]m)?"
_DATE_TEXT = re.compile(
    rf"(\d{{1,2}})(?:st|nd|rd|th)?[ -]*([A-Za-z]{{3,9}})\.?,?[ -]*(\d{{4}})(?:{_TIME})?",
    re.IGNORECASE,
)
_DATE_TEXT_US = re.compile(
    rf"([A-Za-z]{{3,9}})\.? (\d{{1,2}}),? (\d{{4}})(?:{_TIME})?", re.IGNORECASE
)
_DATE_NUM = re.compile(rf"(\d{{1,2}})[/-](\d{{1,2}})[/-](\d{{4}}|\d{{2}})(?:{_TIME})?")
_SUCCESS = re.compile(
    r"\b(success(?:ful(?:ly)?)?|completed|payment\s+done|transferred)\b", re.IGNORECASE
)
_PAID_TO = re.compile(r"\b(paid\s+to|sent\s+to)\b", re.IGNORECASE)  # weak: also on failed pages
_FAILED = re.compile(
    r"\b(fail(?:ed|ure)?|declined|cancel+ed|rejected|unsuccessful|reversed|refund(?:ed)?)\b",
    re.IGNORECASE,
)
_PENDING = re.compile(r"\b(pending|processing|in\s+progress|awaiting)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Expected:
    """What the order says the screenshot should show."""

    amount_minor: int
    upi_id: str
    payee_name: str | None
    created_at: datetime  # when the order was made (timezone-aware)
    now: datetime  # timezone-aware


@dataclass(frozen=True, slots=True)
class Analysis:
    verdict: str
    engine: str
    amount_minor: int | None = None  # the amount that matched, else the one most like a payment
    payee: str | None = None  # a UPI id read from the screenshot
    utr: str | None = None
    paid_at: datetime | None = None  # as printed (India time), naive
    success_marker: bool | None = None
    findings: tuple[str, ...] = field(default_factory=tuple)


def _minor(raw: str) -> int | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    minor = int(value * 100)
    return minor if 1 <= minor <= 100_000_000 else None


_CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?", re.IGNORECASE)
_LONG_ID = re.compile(r"[A-Za-z]{0,4}\d{9,}")  # UTR, phone number, card / account number
# A line that is only a number, optionally with ONE leading glyph: the big headline amount. OCR
# usually turns the rupee sign into some other glyph ("T1", "<1", "~99") or drops it ("1").
_HEADLINE = re.compile(rf"^\s*[^\w\s]?\s?[A-Za-z]?\s?{_NUM}\s*$", re.MULTILINE)


def _mask_noise(text: str) -> str:
    """The text with everything that is not an amount blanked out: dates, clock times, UPI ids and
    long reference numbers. Otherwise "Transferred 29 Oct 2026" reads as a payment of Rs 29."""
    for pattern in (_UPI_RE, _DATE_TEXT, _DATE_TEXT_US, _DATE_NUM, _CLOCK, _LONG_ID):
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _amounts(text: str) -> tuple[list[int], list[int], list[int]]:
    """(amounts with a currency sign / label, lone-number headline lines, other two-decimal
    amounts). Dates, times, ids and reference numbers are removed first."""
    clean = _mask_noise(text)
    marked = [
        m
        for pattern in (_MARKED_AMOUNT, _LABELLED_AMOUNT)
        for raw in pattern.findall(clean)
        if (m := _minor(raw)) is not None
    ]
    headline = [m for raw in _HEADLINE.findall(clean) if (m := _minor(raw)) is not None]
    plain = [m for raw in _PLAIN_AMOUNT.findall(clean) if (m := _minor(raw)) is not None]
    return marked, headline, plain


def _symbol_prefixed(found: int, wanted: int) -> bool:
    """`found` is `wanted` with one extra leading digit: OCR often reads the rupee sign as a digit
    ("Rs 99" -> "799"). That is not a different amount, it is an unreadable one."""
    f, w = str(found // 100), str(wanted // 100)
    return found % 100 == 0 and wanted % 100 == 0 and len(f) == len(w) + 1 and f.endswith(w)


def _time_parts(h: str | None, mi: str | None, ap: str | None) -> tuple[int, int] | None:
    if h is None or mi is None:
        return None
    hour, minute = int(h), int(mi)
    if ap:
        hour = hour % 12 + (12 if ap.lower() == "pm" else 0)
    return (hour, minute) if hour < 24 and minute < 60 else None


def _paid_at(text: str) -> datetime | None:
    """The first date (with its time, when printed) found in the text."""
    candidates: list[tuple[int, datetime]] = []
    for m in _DATE_TEXT.finditer(text):
        month = _MONTHS.get(m.group(2)[:3].lower())
        if month:
            candidates.append((m.start(), _build(m.group(3), month, m.group(1), m.groups()[3:])))
    for m in _DATE_TEXT_US.finditer(text):
        month = _MONTHS.get(m.group(1)[:3].lower())
        if month:
            candidates.append((m.start(), _build(m.group(3), month, m.group(2), m.groups()[3:])))
    for m in _DATE_NUM.finditer(text):
        year = m.group(3) if len(m.group(3)) == 4 else "20" + m.group(3)
        candidates.append((m.start(), _build(year, int(m.group(2)), m.group(1), m.groups()[3:])))
    found = [d for _, d in sorted(candidates, key=lambda c: c[0]) if d is not None]
    return found[0] if found else None


def _build(year: str, month: int, day: str, time_groups: tuple) -> datetime | None:
    hm = _time_parts(*time_groups) if len(time_groups) >= 3 else None
    try:
        return datetime(int(year), month, int(day), *(hm or (0, 0)))
    except ValueError:
        return None


def _time_findings(paid_at: datetime, expected: Expected) -> list[str]:
    """A screenshot older than the order (or from the future) is not evidence of THIS payment."""
    order = expected.created_at.replace(tzinfo=None) + IST
    now = expected.now.replace(tzinfo=None) + IST
    if paid_at.hour == 0 and paid_at.minute == 0:  # only a date was printed
        if paid_at.date() < order.date() - timedelta(days=1):
            return ["time_too_old"]
        return ["time_in_future"] if paid_at.date() > now.date() + timedelta(days=1) else []
    if paid_at < order - ORDER_SLACK:
        return ["time_too_old"]
    return ["time_in_future"] if paid_at > now + ORDER_SLACK else []


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _payee_matches_name(text: str, name: str | None) -> bool:
    tokens = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if len(t) >= 3]
    haystack = _norm(text)
    return bool(tokens) and all(t in haystack for t in tokens)


def analyze_text(text: str, expected: Expected, engine: str = "ocr") -> Analysis:
    """Everything that can be said about the screenshot from its text. Never raises."""
    letters = sum(c.isalnum() for c in text)
    if letters < MIN_TEXT_CHARS:
        return Analysis(UNREADABLE, engine, findings=("no_text",))

    findings: list[str] = []
    marked, headline, plain = _amounts(text)
    seen = marked or headline or plain
    amount = None
    candidates = list(dict.fromkeys(marked + headline + plain))
    if expected.amount_minor in candidates:
        amount = expected.amount_minor
    elif candidates:
        amount = candidates[0]
        if len(candidates) > 1 or any(
            _symbol_prefixed(c, expected.amount_minor) for c in candidates
        ):
            findings.append("amount_unclear")  # several amounts, or a rupee sign read as a digit
        else:
            findings.append("amount_mismatch" if marked or headline else "amount_not_found")
    else:
        findings.append("amount_not_found")

    ids = [m.lower() for m in _UPI_RE.findall(text)]
    want = expected.upi_id.lower()
    payee: str | None = None
    if want in ids:
        payee = want
    elif _payee_matches_name(text, expected.payee_name):
        payee = None  # recognised by name; no id printed
    elif ids:
        close = max(ids, key=lambda i: SequenceMatcher(None, i, want).ratio())
        payee = close[:80]
        findings.append(
            "payee_unclear"
            if SequenceMatcher(None, close, want).ratio() >= 0.85
            else "payee_mismatch"
        )
    else:
        findings.append("payee_not_found")

    utr_match = _UTR_LABELLED.search(text)
    utr = None
    if utr_match and any(c.isdigit() for c in utr_match.group(1)):
        utr = utr_match.group(1).upper()
    elif bare := _UTR_BARE.search(text):
        utr = bare.group(1)

    paid_at = _paid_at(text)
    if paid_at is not None:
        findings.extend(_time_findings(paid_at, expected))

    failed = bool(_FAILED.search(text))
    pending = bool(_PENDING.search(text))
    success = bool(_SUCCESS.search(text)) or (
        bool(_PAID_TO.search(text)) and not failed and not pending
    )
    if failed and not success:
        findings.append("payment_failed")
    elif pending and not success:
        findings.append("payment_pending")
    elif not success and utr is None:
        findings.append("success_not_shown")

    informative = bool(seen or ids or utr or paid_at or success or payee or failed or pending)
    if not informative and not _payee_matches_name(text, expected.payee_name):
        return Analysis(UNREADABLE, engine, findings=("no_payment_info",))

    verdict = OK
    if any(f in MISMATCH_CODES for f in findings):
        verdict = MISMATCH
    elif any(f in WARNING_CODES for f in findings):
        verdict = WARNING
    marker = False if failed and not success else (True if success else None)
    return Analysis(
        verdict, engine, amount, payee, utr, paid_at, marker, tuple(dict.fromkeys(findings))
    )


def failed_analysis(engine: str, reason: str) -> Analysis:
    """The screenshot could not be read at all (OCR missing, crashed, timed out)."""
    return Analysis(UNREADABLE, engine, findings=(reason,))


def utr_check(screenshot_utr: str | None, typed_utr: str | None) -> str:
    """match | mismatch | unknown: the UTR on the screenshot against the one the user typed."""
    if not screenshot_utr or not typed_utr:
        return "unknown"
    return "match" if screenshot_utr == typed_utr else "mismatch"
