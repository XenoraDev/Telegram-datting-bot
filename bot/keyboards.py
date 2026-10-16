"""Keyboards and the (strictly validated) callback-data formats.

Callback data never contains a Telegram user id: chats are referred to by session id and the
server derives the other participant from membership (so a partner's identity can't leak
through button payloads, and a forged payload can't target an arbitrary user).
"""

from __future__ import annotations

import re
from functools import cache

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from . import domain
from .texts import i18n

tr = i18n.tr


def labels(key: str) -> frozenset[str]:
    """The text of catalog `key` in EVERY language: used to recognise a pressed reply-keyboard
    button whatever language it was shown in (button text is display only, never an identifier)."""
    return frozenset(tr(key, lang) for lang in i18n.SUPPORTED)


# English labels (the baseline; also what tests and docs refer to).
BTN_CHAT = tr("btn.chat", "en")
BTN_PROFILE = tr("btn.profile", "en")
BTN_BROWSE = tr("btn.browse", "en")
BTN_MATCHES = tr("btn.matches", "en")
BTN_LANGUAGE = tr("btn.language", "en")
BTN_PREMIUM = tr("btn.premium", "en")
BTN_NEXT = tr("btn.next", "en")
BTN_STOP = tr("btn.stop", "en")
BTN_REPORT = tr("btn.report", "en")

_LOOKING_EMOJI = {"m": "👨", "f": "👩", "any": "👥"}


def gender_label(code: str, lang: str) -> str:
    return f"{domain.GENDER_EMOJI[code]} {tr(f'gender.{code}', lang)}"


def looking_label(code: str, lang: str) -> str:
    return f"{_LOOKING_EMOJI[code]} {tr(f'looking.{code}', lang)}"


def country_label(code: str, lang: str) -> str:
    return f"{domain.flag(code)} {tr(f'country.{code}', lang)}"


# label (in any supported language) -> stored code
GENDER_LABELS = {gender_label(c, lg): c for lg in i18n.SUPPORTED for c in domain.GENDERS}
LOOKING_LABELS = {looking_label(c, lg): c for lg in i18n.SUPPORTED for c in domain.LOOKING_FOR}
COUNTRY_LABELS = {country_label(c, lg): c for lg in i18n.SUPPORTED for c in domain.COUNTRIES}
# English-only views (baseline)
GENDER_BUTTONS = {gender_label(c, "en"): c for c in domain.GENDERS}
LOOKING_BUTTONS = {looking_label(c, "en"): c for c in domain.LOOKING_FOR}
COUNTRY_BUTTONS = {country_label(c, "en"): c for c in domain.COUNTRIES}
INDIA_CITIES = ("Delhi", "Mumbai", "Bengaluru", "Kolkata", "Chennai", "Hyderabad", "Pune", "Noida")

remove = ReplyKeyboardRemove()


def _reply(rows: list[list[str]], *, one_time: bool = False) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=x) for x in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=one_time,
    )


@cache
def main_menu(lang: str) -> ReplyKeyboardMarkup:
    return _reply(
        [
            [tr("btn.chat", lang), tr("btn.browse", lang)],
            [tr("btn.matches", lang), tr("btn.profile", lang)],
            [tr("btn.premium", lang), tr("btn.language", lang)],
        ]
    )


@cache
def chat_menu(lang: str) -> ReplyKeyboardMarkup:
    return _reply([[tr("btn.next", lang), tr("btn.stop", lang), tr("btn.report", lang)]])


@cache
def gender_kb(lang: str) -> ReplyKeyboardMarkup:
    return _reply([[gender_label(c, lang) for c in domain.GENDERS]], one_time=True)


@cache
def looking_kb(lang: str) -> ReplyKeyboardMarkup:
    return _reply([[looking_label(c, lang) for c in domain.LOOKING_FOR]], one_time=True)


@cache
def country_kb(lang: str) -> ReplyKeyboardMarkup:
    items = [country_label(c, lang) for c in domain.COUNTRIES]
    return _reply([items[i : i + 2] for i in range(0, len(items), 2)], one_time=True)


india_city_kb = _reply(  # city names are proper nouns: not translated
    [list(INDIA_CITIES[i : i + 4]) for i in range(0, len(INDIA_CITIES), 4)], one_time=True
)


def _inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=a, callback_data=b) for a, b in row] for row in rows
        ]
    )


def age_gate(lang: str) -> InlineKeyboardMarkup:
    return _inline(
        [[(tr("age_confirm_btn", lang), "age:yes")], [(tr("age_deny_btn", lang), "age:no")]]
    )


def delete_confirm(lang: str) -> InlineKeyboardMarkup:
    return _inline([[(tr("delete_yes_btn", lang), "del:yes"), (tr("btn.cancel", lang), "del:no")]])


def after_chat(session_id: int, lang: str) -> InlineKeyboardMarkup:
    return _inline(
        [
            [
                (tr("btn.report_chat", lang), f"rep:{session_id}"),
                (tr("block_btn", lang), f"blk:{session_id}"),
            ]
        ]
    )


def report_reasons(session_id: int, lang: str) -> InlineKeyboardMarkup:
    rows = [
        [(tr(f"report_reason.{r}", lang), f"rr:{session_id}:{r}")] for r in domain.REPORT_REASONS
    ]
    rows.append([(tr("btn.cancel", lang), "cancel")])
    return _inline(rows)


def browse_card(pid: str, lang: str) -> InlineKeyboardMarkup:
    return _inline(
        [
            [(tr("like_btn", lang), f"lk:{pid}"), (tr("skip_btn", lang), f"ps:{pid}")],
            [(tr("report_btn", lang), f"rp:{pid}"), (tr("block_btn", lang), f"bk:{pid}")],
        ]
    )


def match_card(pid: str, lang: str) -> InlineKeyboardMarkup:
    """Under a card in the matches list ("m" variants: no new card is sent afterwards)."""
    return _inline([[(tr("report_btn", lang), f"mr:{pid}"), (tr("block_btn", lang), f"mk:{pid}")]])


def match_actions(lang: str) -> InlineKeyboardMarkup:
    return _inline([[(tr("matches_btn", lang), "mm"), (tr("browse_btn", lang), "br")]])


def profile_report_reasons(pid: str, prefix: str, lang: str) -> InlineKeyboardMarkup:
    rows = [
        [(tr(f"report_reason.{r}", lang), f"{prefix}:{pid}:{r}")] for r in domain.REPORT_REASONS
    ]
    rows.append([(tr("btn.cancel", lang), "cancel")])
    return _inline(rows)


_SESSION_CB = re.compile(r"^(rep|blk):([0-9]{1,18})$")
_REASON_CB = re.compile(r"^rr:([0-9]{1,18}):([a-z]{3,12})$")


_PROFILE_CB = re.compile(r"^(lk|ps|rp|bk|mr|mk):([0-9a-f]{32})$")
_PROFILE_REASON_CB = re.compile(r"^(rq|rm):([0-9a-f]{32}):([a-z]{3,12})$")


def parse_profile_cb(data: str | None) -> tuple[str, str] | None:
    """(action, profile handle) for like / pass / report / block buttons."""
    m = _PROFILE_CB.match(data or "")
    return (m.group(1), m.group(2)) if m else None


def parse_profile_reason_cb(data: str | None) -> tuple[str, str, str] | None:
    m = _PROFILE_REASON_CB.match(data or "")
    return (m.group(1), m.group(2), m.group(3)) if m else None


def language_choice() -> InlineKeyboardMarkup:
    return _inline([[(label, f"lg:{code}") for code, label in i18n.LANGUAGES.items()]])


def parse_language_cb(data: str | None) -> str | None:
    """Language code of a `lg:<code>` button if it is a supported one, else None."""
    code = (data or "")[3:] if (data or "").startswith("lg:") else None
    return code if code in i18n.SUPPORTED else None


def admin_home(open_reports: int) -> InlineKeyboardMarkup:
    return _inline(
        [
            [("👥 Users", "ad:find")],
            [(f"🚨 Reports ({open_reports})", "ad:rl:0")],
            [("📊 Statistics", "ad:st")],
        ]
    )


def admin_back() -> InlineKeyboardMarkup:
    return _inline([[("⬅️ Back", "ad:home")]])


def admin_user(uid: int, status: str, open_reports: int) -> InlineKeyboardMarkup:
    rows = []
    if status in ("onboarding", "active"):
        rows.append([("🔨 Ban", f"ad:b:{uid}:0")])
    elif status == "banned":
        rows.append([("✅ Unban", f"ad:ub:{uid}")])
    rows.append([("⬅️ Back", "ad:home")])
    return _inline(rows)


def admin_ban_confirm(uid: int, report_id: int) -> InlineKeyboardMarkup:
    back = f"ad:r:{report_id}" if report_id else f"ad:u:{uid}"
    return _inline([[("✅ Confirm ban", f"ad:bo:{uid}:{report_id}"), ("Cancel", back)]])


def admin_reports(rows: list[tuple[int, str, str]], page: int, pages: int) -> InlineKeyboardMarkup:
    """rows: (report id, reason, source)."""
    kb_rows = [[(f"#{rid} · {reason} · {source}", f"ad:r:{rid}")] for rid, reason, source in rows]
    nav = []
    if page > 0:
        nav.append(("◀️", f"ad:rl:{page - 1}"))
    if page < pages - 1:
        nav.append(("▶️", f"ad:rl:{page + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([("⬅️ Back", "ad:home")])
    return _inline(kb_rows)


def admin_report(rid: int, reported_id: int, reported_status: str) -> InlineKeyboardMarkup:
    rows = [[("✅ Resolve", f"ad:rv:{rid}"), ("🗑 Dismiss", f"ad:rd:{rid}")]]
    if reported_status in ("onboarding", "active"):
        rows.append([("🔨 Ban reported user", f"ad:b:{reported_id}:{rid}")])
    rows.append([("👤 User", f"ad:u:{reported_id}")])
    rows.append([("⬅️ Reports", "ad:rl:0")])
    return _inline(rows)


_ADMIN_VERBS = frozenset({"home", "find", "st", "rl", "u", "r", "b", "bo", "ub", "rv", "rd"})
_ADMIN_CB = re.compile(r"^ad:([a-z]{1,4})(?::([0-9]{1,16}))?(?::([0-9]{1,16}))?$")


def parse_admin_cb(data: str | None) -> tuple[str, int | None, int | None] | None:
    """(verb, first number, second number) of an admin button, or None for anything else.

    Shape only: authorization is never derived from the payload (see handlers/admin.py)."""
    m = _ADMIN_CB.match(data or "")
    if not m or m.group(1) not in _ADMIN_VERBS:
        return None
    a = int(m.group(2)) if m.group(2) is not None else None
    b = int(m.group(3)) if m.group(3) is not None else None
    return m.group(1), a, b


def parse_session_cb(data: str | None) -> tuple[str, int] | None:
    m = _SESSION_CB.match(data or "")
    return (m.group(1), int(m.group(2))) if m else None


def parse_reason_cb(data: str | None) -> tuple[int, str] | None:
    m = _REASON_CB.match(data or "")
    return (int(m.group(1)), m.group(2)) if m else None


# ---------------------------------------------------------------- premium / manual payments (P6)
# Callback data: pm:<verb>:<number>. The number is a plan id (verb "s") or the user's OWN payment
# id (everything else); the server checks ownership and state on every press.
_PREMIUM_CB = re.compile(r"^pm:([suck]):([0-9]{1,12})$")


def premium_plans(plans: list[tuple[int, str, str, int]], lang: str) -> InlineKeyboardMarkup:
    """plans: (plan id, name, amount text, days)."""
    return _inline(
        [
            [(tr("btn.plan", lang, name=name, amount=amount, days=days), f"pm:s:{plan_id}")]
            for plan_id, name, amount, days in plans
        ]
    )


def payment_actions(payment_id: int, lang: str) -> InlineKeyboardMarkup:
    return _inline(
        [
            [(tr("btn.upload_proof", lang), f"pm:u:{payment_id}")],
            [(tr("btn.cancel_payment", lang), f"pm:c:{payment_id}")],
        ]
    )


def skip_utr(payment_id: int, lang: str) -> InlineKeyboardMarkup:
    return _inline([[(tr("btn.skip_utr", lang), f"pm:k:{payment_id}")]])


def parse_premium_cb(data: str | None) -> tuple[str, int] | None:
    m = _PREMIUM_CB.match(data or "")
    return (m.group(1), int(m.group(2))) if m else None
