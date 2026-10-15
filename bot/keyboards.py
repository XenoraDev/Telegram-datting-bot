from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BTN_CHAT = "💬 Random Chat"
BTN_BROWSE = "❤️ Browse Profiles"
BTN_MATCHES = "💕 My Matches"
BTN_PROFILE = "👤 My Profile"
BTN_NEXT = "⏭ Next"
BTN_STOP = "⏹ Stop"

remove = ReplyKeyboardRemove()


def _kb(rows, **kw):
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True, **kw
    )


main_menu = _kb([[BTN_CHAT, BTN_BROWSE], [BTN_MATCHES, BTN_PROFILE]])
chat_menu = _kb([[BTN_NEXT, BTN_STOP]])
gender_kb = _kb([["👨 Male", "👩 Female", "🧑 Other"]], one_time_keyboard=True)
looking_kb = _kb([["👨 Men", "👩 Women", "👥 Everyone"]], one_time_keyboard=True)

GENDERS = {"👨 Male": "m", "👩 Female": "f", "🧑 Other": "o"}
LOOKING = {"👨 Men": "m", "👩 Women": "f", "👥 Everyone": "any"}
GENDER_LABEL = {"m": "👨 Male", "f": "👩 Female", "o": "🧑 Other"}


def swipe_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❤️ Like", callback_data=f"like:{uid}"),
                InlineKeyboardButton(text="👎 Skip", callback_data=f"skip:{uid}"),
            ],
            [
                InlineKeyboardButton(text="🚩 Report", callback_data=f"report:{uid}"),
                InlineKeyboardButton(text="🛑 Stop browsing", callback_data="stopbrowse"),
            ],
        ]
    )


def after_chat_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❤️ Like", callback_data=f"like:{uid}"),
                InlineKeyboardButton(text="🚩 Report", callback_data=f"report:{uid}"),
                InlineKeyboardButton(text="🚫 Block", callback_data=f"block:{uid}"),
            ]
        ]
    )
