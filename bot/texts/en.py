"""All user-facing English strings. HTML parse mode is the bot default, so anything user-supplied
that gets interpolated MUST go through html.escape (use `esc`)."""

from html import escape as esc  # re-exported for handlers

__all__ = ["esc"]

AGE_GATE = (
    "👋 <b>Welcome!</b>\n\n"
    "This bot is <b>only for adults (18+)</b>: profiles and anonymous chat with other adults.\n\n"
    "Please confirm your age to continue. By continuing you agree to be respectful, not to "
    "share personal contact details, and not to send sexual or illegal content."
)
AGE_CONFIRM_BTN = "✅ I am 18 or older"
AGE_DENY_BTN = "❌ I am under 18"

BLOCKED_ADULTS_ONLY = (
    "⛔ Sorry, this service is for adults (18+) only, so you can't use it. "
    "Send /deleteme if you want your data removed."
)
BLOCKED_BANNED = "⛔ Your account is restricted. You can't use this bot."
ACCOUNT_DELETED = "🗑 Your account has been deleted. Send /start to create a new one."
NEED_START = "Please send /start first."
FINISH_SETUP = "Please finish your profile first: send /start."
UNKNOWN = "I didn't understand that. Use the menu buttons, or /help."

ASK_NAME = "What name should we show? (1-30 characters, no links or contact details)"
ASK_AGE = "How old are you? (a number, you must be 18+)"
ASK_GENDER = "You are…"
ASK_LOOKING = "Who would you like to chat with?"
ASK_CITY = "Which city are you in? (up to 40 characters)"
ASK_BIO = "Write a short bio (up to 300 characters, no links or contact details)."
USE_BUTTONS = "Please use the buttons below."
SEND_TEXT = "Please send a text message."
PROFILE_SAVED = "✅ Profile saved! You can now start a random chat."
PROFILE_RESTART = "Something was off with your answers, let's start the profile again."
PROFILE_CANCELLED = "Cancelled."
EDIT_NOT_IN_CHAT = "Leave your current chat first (/stop), then edit your profile."
EDIT_NEEDS_PROFILE = "You don't have a profile yet: send /start."

INVALID = {
    "name_length": "The name must be 1-30 characters.",
    "name_chars": "The name contains unsupported characters.",
    "city_length": "The city must be 1-40 characters.",
    "city_chars": "The city contains unsupported characters.",
    "bio_length": "The bio must be 1-300 characters.",
    "bio_chars": "The bio contains unsupported characters.",
    "age_not_number": "Please send your age as a number, e.g. 25.",
    "age_range": "Please enter a realistic age between 18 and 80.",
    "text_link": "Links aren't allowed here. Please rewrite it.",
    "text_contact": "Please don't include @usernames, e-mails or phone numbers. Rewrite it.",
    "text_sexual": "That text isn't allowed (sexual / solicitation content). Please rewrite it.",
    "text_minor": (
        "That text isn't allowed. This service is for adults only; "
        "please don't mention ages under 18 or similar topics."
    ),
    "gender": "Please choose one of the buttons.",
    "looking_for": "Please choose one of the buttons.",
}

MENU_HINT = "Choose an option below."
WELCOME_BACK = "Welcome back! 👋 Tap 💬 Random Chat to meet someone."
HELP = (
    "<b>Commands</b>\n"
    "/chat – find a random chat partner\n"
    "/next – skip to someone new\n"
    "/stop – leave the chat\n"
    "/report – report your current partner\n"
    "/block – block your current partner\n"
    "/profile – see your profile\n"
    "/editprofile – change your profile\n"
    "/deleteme – delete your account and data\n\n"
    "In chats only <b>text and stickers</b> work. Links, e-mails and phone numbers are blocked."
)

SEARCHING = "🔎 Looking for a partner… /stop to cancel."
ALREADY_SEARCHING = "Still searching… /stop to cancel."
ALREADY_IN_CHAT = "You're already in a chat. Use /next or /stop."
SEARCH_RATE_LIMITED = "⏳ You're searching too fast. Wait a moment."
NOT_ELIGIBLE = "Finish your profile first: send /start."
COOLDOWN = "⏳ Random chat is paused for your account for a while because of reports."
MATCHED = (
    "🎉 <b>Partner found!</b>\n"
    "{age} · {gender} · 📍 {city}\n\n"
    "Chat is anonymous: <b>text and stickers only</b>; links, e-mails and phone numbers "
    "are blocked. Be kind. /next · /stop · /report"
)
SEARCH_EXPIRED = "😕 No partner found for now, the search ended. Tap 💬 Random Chat to retry."
CHAT_ENDED_ME = "Chat ended."
CHAT_ENDED_PARTNER = "😔 Your partner left the chat."
SEARCH_CANCELLED = "Search cancelled."
NOT_IN_CHAT = "You're not in a chat. Tap 💬 Random Chat."
PARTNER_UNREACHABLE = "⚠️ Your partner can't be reached anymore. The chat was ended."
AFTER_CHAT = "How was your chat? You can report or block your last partner."

UNSUPPORTED = (
    "⚠️ Only text and stickers are allowed. Photos, files, voice, links and forwards are blocked."
)
LINK_BLOCKED = "⚠️ Links aren't allowed in chat."
CONTACT_BLOCKED = "⚠️ Sharing e-mails or phone numbers isn't allowed."
TOO_LONG = "⚠️ That message is too long (max 1000 characters)."
FLOOD = "⚠️ Please don't repeat the same message."
CHAT_RATE_LIMITED = "⏳ You're sending too fast."
MUTED = "🔇 You are temporarily muted because of rule violations."
SLOW_DOWN = "⏳ Slow down a little."

REPORT_START = "Why are you reporting your partner?"
REPORT_CREATED = "✅ Thanks, your report was received. You won't be matched with this person again."
REPORT_DUPLICATE = "You already reported this chat."
REPORT_RATE_LIMITED = "You've sent too many reports recently. Please try again later."
REPORT_NOT_FOUND = "That chat can't be reported (too old or not yours)."
BLOCK_DONE = "🚫 Blocked. You won't be matched with this person again."
REPORT_REASON_LABEL = {
    "spam": "Spam",
    "harassment": "Harassment",
    "sexual": "Sexual content",
    "minor": "Seems under 18",
    "scam": "Scam",
    "other": "Other",
}

DELETE_CONFIRM = (
    "⚠️ <b>Delete your account?</b>\nYour profile and personal data are removed and any chat "
    "ends. Safety records (reports / blocks against you) are kept. This can't be undone."
)
DELETE_YES_BTN = "🗑 Yes, delete everything"
DELETE_NO_BTN = "Cancel"
DELETE_DONE = "🗑 Your account and data were deleted. Send /start to join again as a new user."
DELETE_DONE_RESTRICTED = "Your personal data was removed. This account stays blocked."
DELETE_ALREADY = "Your account is already deleted. Send /start to create a new one."
DELETE_CANCELLED = "OK, nothing was deleted."
DELETE_RATE_LIMITED = "⏳ Please wait a bit before asking again."
