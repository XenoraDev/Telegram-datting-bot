"""Keyboards and the (strictly validated) callback-data formats.

Callback data never contains a Telegram user id: chats are referred to by session id and the
server derives the other participant from membership (so a partner's identity can't leak
through button payloads, and a forged payload can't target an arbitrary user).
"""

from __future__ import annotations

import re

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from . import domain
from .texts import en as t

BTN_CHAT = "💬 Random Chat"
BTN_PROFILE = "👤 My Profile"
BTN_NEXT = "⏭ Next"
BTN_STOP = "⏹ Stop"
BTN_REPORT = "🚩 Report"

GENDER_BUTTONS = {"👨 Male": "m", "👩 Female": "f", "🧑 Other": "o"}
LOOKING_BUTTONS = {"👨 Men": "m", "👩 Women": "f", "👥 Everyone": "any"}

remove = ReplyKeyboardRemove()


def _reply(rows: list[list[str]], *, one_time: bool = False) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=x) for x in row] for row in rows],
        resize_keyboard=True,
        one_time_keyboard=one_time,
    )


main_menu = _reply([[BTN_CHAT, BTN_PROFILE]])
chat_menu = _reply([[BTN_NEXT, BTN_STOP, BTN_REPORT]])
gender_kb = _reply([list(GENDER_BUTTONS)], one_time=True)
looking_kb = _reply([list(LOOKING_BUTTONS)], one_time=True)


def _inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=a, callback_data=b) for a, b in row] for row in rows
        ]
    )


def age_gate() -> InlineKeyboardMarkup:
    return _inline([[(t.AGE_CONFIRM_BTN, "age:yes")], [(t.AGE_DENY_BTN, "age:no")]])


def delete_confirm() -> InlineKeyboardMarkup:
    return _inline([[(t.DELETE_YES_BTN, "del:yes"), (t.DELETE_NO_BTN, "del:no")]])


def after_chat(session_id: int) -> InlineKeyboardMarkup:
    return _inline([[("🚩 Report", f"rep:{session_id}"), ("🚫 Block", f"blk:{session_id}")]])


def report_reasons(session_id: int) -> InlineKeyboardMarkup:
    rows = [[(t.REPORT_REASON_LABEL[r], f"rr:{session_id}:{r}")] for r in domain.REPORT_REASONS]
    rows.append([("Cancel", "cancel")])
    return _inline(rows)


_SESSION_CB = re.compile(r"^(rep|blk):([0-9]{1,18})$")
_REASON_CB = re.compile(r"^rr:([0-9]{1,18}):([a-z]{3,12})$")


def parse_session_cb(data: str | None) -> tuple[str, int] | None:
    m = _SESSION_CB.match(data or "")
    return (m.group(1), int(m.group(2))) if m else None


def parse_reason_cb(data: str | None) -> tuple[int, str] | None:
    m = _REASON_CB.match(data or "")
    return (int(m.group(1)), m.group(2)) if m else None
