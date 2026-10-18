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
ASK_COUNTRY = "Which country are you in?"
ASK_CITY = "Your city or region? Tap one or type it (e.g. Delhi). No address."
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
    "city_chars": "Please send just a city or region name (no numbers or symbols).",
    "bio_length": "The bio must be 1-300 characters.",
    "bio_chars": "The bio contains unsupported characters.",
    "age_not_number": "Please send your age as a number, e.g. 25.",
    "age_range": "Please enter a realistic age between 18 and 80.",
    "text_link": "Links aren't allowed here. Please rewrite it.",
    "text_contact": "Please don't include @usernames, e-mails or phone numbers. Rewrite it.",
    "text_sexual": "That text isn't allowed (sexual / solicitation content). Please rewrite it.",
    "text_abuse": "That text has abusive words, which aren't allowed. Please rewrite it.",
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
    "/browse – browse profiles, follow or skip\n"
    "/friends – your friends (you follow each other)\n"
    "/next – skip to someone new\n"
    "/stop – leave the chat\n"
    "/report – report your current partner\n"
    "/block – block your current partner\n"
    "/language – choose English or Hindi\n"
    "/profile – see your profile\n"
    "/editprofile – change your profile\n"
    "/privacy – choose what partners can see\n"
    "/followers – who follows you\n"
    "/filters – advanced filters (Pro/VIP)\n"
    "/boost – profile boost (Pro/VIP)\n"
    "/premium – subscriptions\n"
    "/myplan – your plan, days left and benefits\n"
    "/payments – your payment history\n"
    "/support – write to support\n"
    "/tickets – your support tickets\n"
    "/terms – rules\n"
    "/about – about this bot and your data\n"
    "/deleteme – delete your account and data\n\n"
    "In chats only <b>text and stickers</b> work. Links, e-mails and phone numbers are blocked."
)

LIKE_BTN = "➕ Follow"
SKIP_BTN = "⏭️ Skip"
REPORT_BTN = "🚨 Report"
BLOCK_BTN = "🚫 Block"
MATCHES_BTN = "👥 My Friends"
BROWSE_BTN = "🔎 Browse"
CARD = "👤 <b>{name}</b>\n🎂 {age}\n{flag} {city}\n\n📝 {bio}"
MATCH_CARD = "👤 <b>{name}</b>, {age} · {flag} {city}"
BROWSE_EMPTY = "No new profiles right now. Check back later! 🔎"
BROWSE_IN_CHAT = "Leave your current chat first (/stop), then browse profiles."
PROFILE_GONE = "That profile isn't available any more."
MATCH_NEW = "🎉 <b>You are now friends!</b>\n\nYou both follow each other.\n\n{card}"
MATCHES_NONE = "No friends yet. Follow some profiles with 🔎 Browse!"
MATCHES_HEADER = "👥 <b>Your friends</b> ({total})"
MATCHES_MORE = "…and {n} more."
PROFILE_BLOCKED = "🚫 Blocked. You won't see this person again."

ADMIN_DENIED = "⛔ Not authorized."
ADMIN_PAYMENT_NEW = "💳 New payment {reference}: ₹{amount} for {plan}. Review it in the web admin."
ADMIN_TICKET_NEW = "🎫 New support ticket #{id} ({category}). Answer it in the web admin → Support."
ADMIN_TICKET_REPLY = "💬 Support ticket #{id}: the user wrote again. Open the web admin → Support."
ADMIN_ERROR = "⚠️ Bot error: {error} in {where} ({count}×). Details are in the server log."
ADMIN_TELEGRAM_FAILING = (
    "⚠️ Telegram sends keep failing ({kind}, {count}×). Messages to users may be delayed."
)
ADMIN_HOME = "🛠 <b>Admin</b>"
ADMIN_ASK_ID = "Send the Telegram user ID (digits only)."
ADMIN_BAD_ID = "That is not a valid user ID. Send digits only, e.g. 123456789."
ADMIN_NO_USER = "No user with that ID."
ADMIN_NO_REPORT = "That report doesn't exist."
ADMIN_NO_REPORTS = "✅ No open reports."
ADMIN_BUSY = "⏳ Too many admin actions, wait a moment."
ADMIN_PROTECTED = "Admins can't be banned from the bot."
ADMIN_BAD_STATE = "That account can't be changed this way (wrong status)."
ADMIN_ALREADY = "That report was already handled."
ADMIN_BANNED = "🔨 Banned."
ADMIN_UNBANNED = "✅ Unbanned (now: {status})."
ADMIN_REPORT_DONE = "✅ Report closed."

SEARCHING = (
    "🔎 Looking for a partner… I'll message you when someone joins. "
    "Try 🔎 Browse meanwhile, or /stop."
)
ALREADY_SEARCHING = "Still searching… I'll message you when someone joins. /stop to cancel."
ALREADY_IN_CHAT = "You're already in a chat. Use /next or /stop."
SEARCH_RATE_LIMITED = "⏳ You're searching too fast. Wait a moment."
NOT_ELIGIBLE = "Finish your profile first: send /start."
COOLDOWN = "⏳ Random chat is paused for your account for a while because of reports."
MATCHED = "🎉 <b>Partner Found!</b>\n\n{gender} {age} · {flag} {city}\n\n🔒 Anonymous chat"
SEARCH_EXPIRED = (
    "😕 No one was online, so the search ended. Try 💬 Random Chat again later, or 🔎 Browse now."
)
CHAT_ENDED_ME = "Chat ended."
CHAT_ENDED_PARTNER = "😔 Your partner left the chat."
CHAT_ENDED_IDLE = "⌛ The chat ended because nobody wrote for {minutes} minutes."
SEARCH_CANCELLED = "Search cancelled."
NOT_IN_CHAT = "You're not in a chat. Tap 💬 Random Chat."
PARTNER_UNREACHABLE = "⚠️ Your partner can't be reached anymore. The chat was ended."
AFTER_CHAT = "How was your chat? You can report or block your last partner."

UNSUPPORTED = (
    "⚠️ Only text and stickers are allowed. Photos, files, voice, links and forwards are blocked."
)
LINK_BLOCKED = "⚠️ Links aren't allowed in chat."
CONTACT_BLOCKED = "⚠️ Sharing e-mails or phone numbers isn't allowed."
ABUSE_BLOCKED = "⚠️ Please keep the chat respectful. That message was not sent."
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

# ---------------------------------------------------------------------------------------------
# Translation catalog. Every user-facing string has a stable key; `texts/i18n.tr(key, lang)` looks
# it up, `texts/hi.py` holds the Hindi version, and English is the fallback. The module constants
# above are the English wording (kept as constants so existing code and tests can import them);
# the catalog keys are derived from their names: SEARCHING -> "searching", INVALID["gender"] ->
# "invalid.gender", REPORT_REASON_LABEL["spam"] -> "report_reason.spam".
# The admin panel (operator tool) stays English except what a non-admin can see (admin_denied).

_NOT_TRANSLATED = {"DELETE_NO_BTN", "MENU_HINT"}  # replaced by btn.cancel / unused

_EXTRA = {
    "lang.prompt": "🌐 <b>Language</b>\nChoose your language:",
    "lang.changed": "✅ Language set to English.",
    "lang.invalid": "That language isn't available.",
    "lang.unavailable": "Finish setting up your account first: send /start.",
    # menu / chat buttons
    "btn.chat": "💬 Random Chat",
    "btn.browse": "🔎 Browse",
    "btn.matches": "👥 My Friends",
    "btn.profile": "👤 My Profile",
    "btn.language": "🌐 Language",
    "btn.next": "➡️ Next",
    "btn.stop": "🛑 Stop",
    "btn.report": "🚨 Report",
    "btn.cancel": "Cancel",
    "btn.report_chat": "🚩 Report",
    # profile choices (button = emoji + this name; also used on the profile screen)
    "gender.m": "Male",
    "gender.f": "Female",
    "gender.o": "Other",
    "looking.m": "Men",
    "looking.f": "Women",
    "looking.any": "Everyone",
    "country.IN": "India",
    "country.US": "USA",
    "country.GB": "UK",
    "country.PK": "Pakistan",
    "country.BD": "Bangladesh",
    "country.NP": "Nepal",
    "country.AE": "UAE",
    "country.CA": "Canada",
    "country.AU": "Australia",
    "country.ZZ": "Other",
    "profile.view": (
        "<b>{name}</b>, {age}\n{gender} · looking for {looking}\n{flag} {city}\n\n{bio}\n\n"
        "/editprofile to change it"
    ),
    "thanks": "Thanks!",
    # --- P6 premium / manual payments
    "btn.premium": "⭐ Premium",
    "btn.plan": "{name} — ₹{amount} / {days} days",
    "btn.cancel_payment": "✖️ Cancel payment",
    "btn.payments": "🧾 My payments",
    "payments.title": "🧾 <b>Your payments</b> (latest {n})",
    "payments.empty": "You have no payments yet. Open ⭐ Premium to choose a plan.",
    "payments.item": ("{icon} <code>{reference}</code> — {plan}, ₹{amount}\n{status} · {date}"),
    "payments.status_pending": "Waiting for review",
    "payments.status_approved": "Approved",
    "payments.status_rejected": "Not approved",
    "payments.reason": "Reason: {reason}",
    # --- A4 support tickets
    "btn.support": "🆘 Support",
    "support.in_chat": "Finish your chat first (/stop), then open Support.",
    "support.choose": "🆘 <b>Support</b>\nWhat do you need help with?",
    "support.cat.payment": "💳 Payment or Premium",
    "support.cat.account": "👤 My account",
    "support.cat.chat": "💬 A chat problem",
    "support.cat.report": "🚨 A report or block",
    "support.cat.other": "❓ Something else",
    "support.btn_cancel": "✖️ Cancel",
    "support.btn_reply": "💬 Reply",
    "support.btn_close": "✅ Close ticket",
    "support.item_btn": "{icon} #{id} · {category}",
    "support.ask": (
        "✍️ Describe the problem in one message (up to 2000 characters). "
        "Never send passwords or card numbers."
    ),
    "support.ask_reply": "✍️ Send your reply as one message (up to 2000 characters).",
    "support.created": (
        "✅ Your ticket <b>#{id}</b> was sent. We will answer here. See all your tickets: /tickets"
    ),
    "support.reply_sent": "✅ Your reply was sent.",
    "support.cancelled": "Cancelled. Nothing was sent to support.",
    "support.too_many": (
        "You already have 3 open tickets. Close one in /tickets or wait for an answer."
    ),
    "support.empty": "Please send some text.",
    "support.too_long": (
        "That message is too long (2000 characters at most). Shorten it and send again."
    ),
    "support.full": "This ticket has too many messages. Close it and open a new one.",
    "support.not_found": "That ticket was not found.",
    "support.closed_already": "That ticket is already closed.",
    "support.closed_ok": "✅ Ticket #{id} is closed. Need more help? Use /support.",
    "support.none": "You have no tickets yet. Write to us with /support.",
    "support.list_header": "🎫 <b>Your tickets</b> (latest {n}) — tap one to open it.",
    "support.status_open": "waiting for support",
    "support.status_answered": "answered",
    "support.status_closed": "closed",
    "support.view": "🎫 <b>Ticket #{id}</b> · {category}\nStatus: {status}\n\n{thread}",
    "support.from_user": "👤 You",
    "support.from_admin": "🛟 Support",
    "support.admin_replied": (
        "💬 <b>Support answered your ticket #{id}:</b>\n\n{reply}\n\nReply or close it in /tickets."
    ),
    "support.admin_closed": (
        "✅ Support closed your ticket #{id}. You can open a new one with /support."
    ),
    # --- A5 launch gaps
    "error.generic": (
        "⚠️ Something went wrong on our side. Please try again in a moment. "
        "If it keeps happening, write to us via 🆘 Support."
    ),
    "chat.delivery_delayed": (
        "⏳ Telegram is slow right now and your message may not have reached your partner. "
        "Please send it again."
    ),
    "start.pick_language": "🌐 Welcome! Choose your language to begin:",
    "restart.resume": (
        "🔄 Your sign-up is not finished (the bot may have restarted). Let's continue."
    ),
    "terms": (
        "📜 <b>Terms and rules</b>\n\n"
        "• You must be 18 or older. Accounts under 18 are blocked.\n"
        "• Be respectful. Spam, harassment, sexual content, scams and anyone who seems under 18 "
        "are not allowed and can be reported with 🚨 Report. Breaking the rules can lead to a "
        "ban.\n"
        "• Chats are anonymous and text and stickers only. Phone numbers, e-mails and links are "
        "blocked, so please do not try to share them.\n"
        "• You can block or report a partner at any time.\n"
        "• Premium is paid by UPI outside the bot and starts only after an admin has checked "
        "and approved your payment. The plans and limits are shown in ⭐ Premium.\n"
        "• Need help? Use 🆘 Support. Your data: /about"
    ),
    "about": (
        "ℹ️ <b>About this bot and your data</b>\n\n"
        "An anonymous random chat and dating bot for adults (18+).\n\n"
        "<b>What we keep</b>\n"
        "• Chat messages are <b>not</b> stored. For safety the last few messages of a chat stay "
        "in memory for at most 10 minutes. Only if someone reports a chat are they saved with "
        "the report, and they are deleted after 30 days.\n"
        "• Your profile and settings are kept until you delete your account. Partners never see "
        "your username, ID or contact details; age, gender and city are shown as you allow in "
        "/privacy.\n"
        "• Payment records (reference, plan, amount, status) are kept. Payment screenshots are "
        "deleted 90 days after the payment was decided.\n"
        "• Support tickets are kept until you delete your account.\n\n"
        "<b>Your choice</b>\n"
        "/deleteme deletes your profile, follows, blocks and support tickets. Records needed for "
        "safety, such as reports about you and bans, stay.\n"
        "Rules: /terms"
    ),
    # --- /myplan and the "plan ends soon" reminder
    "myplan.title": "📋 <b>My plan</b>",
    "myplan.plan_active": "Plan: <b>{plan}</b>\nEnds: <b>{date}</b> (days left: {days})",
    "myplan.plan_free": "Plan: <b>Free</b>",
    "myplan.queued": "Next: <b>{plan}</b> starts on {date}",
    "myplan.today": "<b>Used today</b>",
    "myplan.l_chat": "💬 Random chats",
    "myplan.l_browse": "👀 Profiles browsed",
    "myplan.l_likes": "➕ Follows",
    "myplan.use_limited": "{label}: {used} / {limit}",
    "myplan.use_unlimited": "{label}: {used} (no limit)",
    "myplan.benefits": "<b>Your benefits</b>",
    "myplan.more": "🔓 Plans and renewals: /premium",
    "reminder.expiring": (
        "⏰ Your <b>{plan}</b> ends on <b>{date}</b> (days left: {days}). "
        "Renew to keep your benefits: /premium"
    ),
    # --- 💬 Chat on a match
    "match_chat_btn": "💬 Chat",
    "match_chat.header": "💬 <b>Chat with your friend</b>",
    "match_chat.already": "💬 You are already chatting with them. Just type your message.",
    "match_chat.busy": "Finish your current chat first (/stop), then tap 💬 Chat again.",
    "match_chat.partner_busy": "They are in another chat right now. Try again a little later.",
    "match_chat.unavailable": "This chat is not available right now.",
    "match_chat.rate": "⏳ Too fast. Wait a moment and try again.",
    # --- "someone followed you"
    "follow_back_btn": "➕ Follow back",
    "follow.new_free": (
        "➕ Someone followed you! People waiting for you: {n}. See the count: /followers · "
        "See who: /premium"
    ),
    "follow.new_pro": "➕ <b>New follower</b>\n\n{card}",
    "premium.header": "⭐ <b>Premium</b>",
    "premium.active": "✅ Your Premium is active until <b>{date}</b>.",
    "premium.choose": "Choose a plan:",
    "premium.free_head": "🆓 <b>Free</b> (what you have now)",
    "premium.plan_head": "⭐ <b>{name}</b> — ₹{amount} / {days} days",
    "benefit.chats": "💬 {n} random chats a day",
    "benefit.chats_unlimited": "💬 Unlimited random chats",
    "benefit.browse": "👀 Browse {n} profiles a day",
    "benefit.browse_unlimited": "👀 Browse unlimited profiles",
    "benefit.likes": "➕ {n} follows a day",
    "benefit.likes_unlimited": "➕ Unlimited follows",
    "benefit.partner": "🔓 See your partner's {fields}",
    "benefit.field_age": "age",
    "benefit.field_gender": "gender",
    "benefit.field_city": "city",
    "benefit.filters": "🎯 Advanced profile filters",
    "benefit.who_liked": "💌 See who follows you",
    "benefit.boost": "🚀 Profile boost",
    "benefit.priority": "⚡ Priority in matching",
    "benefit.badge": "🏅 Plan badge on your profile",
    "premium.none": "Premium plans are not available right now.",
    "premium.unavailable": "Payments are not available right now. Please try again later.",
    "premium.pending": (
        "⏳ Your payment <code>{reference}</code> ({plan}, ₹{amount}) is waiting for "
        "verification. You will get a message here once it has been reviewed."
    ),
    "premium.rejected_note": (
        "❌ Your last payment <code>{reference}</code> was not approved.\nReason: {reason}"
    ),
    "premium.in_chat": "Finish your chat first (/stop), then open Premium.",
    "premium.qr_caption": "Scan to pay ₹{amount} · {reference}",
    "premium.pay": (
        "💳 <b>{plan}</b> — {days} days\n\n"
        "Amount: <b>₹{amount}</b> (pay exactly this)\n"
        "Reference: <code>{reference}</code>\n"
        "UPI ID: <code>{upi}</code>\n"
        "Name: {payee}\n\n"
        "{instructions}"
        "1️⃣ Scan the QR code or pay to the UPI ID above.\n"
        "2️⃣ Then send the payment screenshot here as a photo. Nothing else to tap.\n"
        "3️⃣ Optional: put your UTR / transaction ID in the photo caption, or send it later.\n\n"
        "A screenshot is only proof: Premium starts after an admin has checked and approved "
        "your payment."
    ),
    "premium.ask_screenshot": (
        "📸 Send a screenshot of your payment as a photo (or an image file, up to 3 MB)."
    ),
    "premium.utr_already": "A UTR is already saved for this payment.",
    "premium.utr_saved": "✅ UTR saved. The admin will see it with your payment.",
    "premium.submitted_ask_utr": (
        "✅ Screenshot received. Payment <code>{reference}</code> submitted for verification.\n\n"
        "An admin will check it and you will be notified here. Premium starts only after "
        "approval.\n\n"
        "Optional: send your UTR / transaction ID as a message to help the admin."
    ),
    "premium.already_submitted": (
        "Your payment is already submitted and waiting for an admin. You do not need to send "
        "another screenshot."
    ),
    "premium.no_order": (
        "To pay, first choose a plan with ⭐ Premium. Then send the screenshot here."
    ),
    "premium.screenshot_not_image": (
        "That is not a valid image. Send a JPG, PNG or WebP screenshot."
    ),
    "premium.screenshot_too_big": (
        "That image is too large (3 MB at most). Send a smaller screenshot."
    ),
    "premium.screenshot_corrupt": "That image looks damaged or incomplete. Please send it again.",
    "premium.utr_invalid": (
        "That UTR does not look right. Use only letters and digits (6 to 35 characters)."
    ),
    "premium.utr_duplicate": "This UTR was already used for another payment. Please check it.",
    "premium.submitted": (
        "✅ Payment <code>{reference}</code> submitted for verification.\n\n"
        "An admin will check it and you will be notified here. Premium starts only after approval."
    ),
    "premium.cancelled": "Payment cancelled.",
    "premium.stale": "This payment request is no longer valid. Open ⭐ Premium to start again.",
    "premium.busy": "Please wait a moment and try again.",
    "premium.lower_tier": (
        "ℹ️ You already have a higher plan active, so you can't buy a lower plan right now.\n"
        "You can renew your current plan or upgrade, or buy again after it ends. "
        "See /myplan."
    ),
    "premium.rate_limited": "Too many attempts. Please wait a minute and try again.",
    "premium.approved": (
        "🎉 Payment <code>{reference}</code> approved! Premium is active until <b>{date}</b>."
    ),
    "premium.rejected": (
        "❌ Payment <code>{reference}</code> was not approved.\nReason: {reason}\n\n"
        "You can submit a new payment from ⭐ Premium."
    ),
    # --- P7 daily limits
    "limit.chat": (
        "⏳ You have used all {limit} random chats for today. The limit resets at 00:00 UTC.\n\n"
        "🔓 Want more? /premium"
    ),
    "limit.browse": (
        "⏳ You have viewed {limit} profiles today, the daily limit. It resets at 00:00 UTC.\n\n"
        "🔓 Want more? /premium"
    ),
    "limit.like": (
        "⏳ You have used all {limit} follows for today. The limit resets at 00:00 UTC.\n\n"
        "🔓 Want more? /premium"
    ),
    "browse_rate_limited": "Slow down a little: try again in a few seconds.",
    # --- P7 partner card + privacy
    "pc.rule": "━━━━━━━━━━━━━━━━━━",
    "pc.title": "👤 <b>Partner Profile</b>",
    "pc.matched": "🎉 <b>Partner Found!</b>",
    "followers.line": "👥 Followers: {n}",
    "pc.name": "Name: {value}",
    "pc.age": "Age: {value}",
    "pc.gender": "Gender: {value}",
    "pc.city": "City: {value}",
    "pc.country": "Country: {value}",
    "pc.looking": "Looking for: {value}",
    "pc.about": "About: {value}",
    "pc.locked": "••",
    "btn.reveal": "👁 Show age & gender",
    "pc.reveal_ended": "This chat has ended, so there is nothing to show.",
    "pc.reveal_locked": "🔒 Age and gender are a Premium feature. See the plans below.",
    "pc.reveal_cta": "To subscribe, end this chat first (/stop), then open ⭐ Premium.",
    "pc.hidden": "🙈 Hidden by the user",
    "pc.badge.1": "⭐ Premium",
    "pc.badge.2": "💎 Pro",
    "pc.badge.3": "👑 VIP",
    "pc.anon": "🔒 Anonymous chat",
    "pc.cta": "🔓 Subscribe to unlock partner details: /premium",
    "privacy.header": (
        "🔒 <b>Privacy</b>\n\nChoose what your partners can see. A subscription only lets people "
        "see what you allow here; nobody can override your choice."
    ),
    "privacy.field.age": "Age",
    "privacy.field.gender": "Gender",
    "privacy.field.city": "City",
    "privacy.on": "✅ Shown",
    "privacy.off": "🙈 Hidden",
    "privacy.no_profile": "Create your profile first: /start",
    "privacy.in_chat": "Tap a button to change what partners can see.",
    "premium.queued": (
        "🎉 Payment <code>{reference}</code> approved! Your new plan starts on <b>{start}</b>, "
        "when your current plan ends, and runs until <b>{date}</b>."
    ),
    "likes.none": "No new followers are waiting right now. Keep browsing with 🔎 Browse!",
    "likes.header": "➕ <b>People who follow you</b> ({n})",
    "likes.locked": (
        "➕ {n} people follow you and are waiting for you to follow back.\n\n"
        "🔒 Subscribe to Pro to see who they are: /premium"
    ),
    # --- P8 advanced filters
    "filters.btn": "🎯 Filters",
    "filters.locked": (
        "🔒 Advanced Filters are available with Pro or VIP.\n\nSubscribe to filter profiles by "
        "age, gender, city and what people are looking for: /premium"
    ),
    "filters.field_locked": "🔒 not included in your plan",
    "filters.field_locked_msg": "🔒 Your plan does not include this filter.",
    "filters.header": (
        "🎯 <b>Advanced Filters</b>\n\nOnly profiles that match all your filters are shown."
    ),
    "filters.row.age": "🎂 Age: {value}",
    "filters.row.gender": "⚧ Gender: {value}",
    "filters.row.city": "🏙 City: {value}",
    "filters.row.looking": "💞 Looking for: {value}",
    "filters.any": "Any",
    "filters.age": "🎂 Age",
    "filters.gender": "⚧ Gender",
    "filters.city": "🏙 City",
    "filters.looking": "💞 Looking for",
    "filters.reset": "♻️ Reset filters",
    "filters.done": "✅ Done",
    "filters.back": "⬅️ Back",
    "filters.pick_age": "🎂 Choose an age range:",
    "filters.pick_gender": "⚧ Show only this gender:",
    "filters.pick_looking": "💞 Show people looking for:",
    "filters.ask_city": "🏙 Type the city you want to see (for example: Pune).",
    "filters.reset_done": "Filters cleared",
    "filters.note": (
        "People who hide their age, gender or city never match a filter on that field."
    ),
    "invalid.filter_age": "That age range is not valid.",
    "invalid.filter_choice": "Please choose one of the buttons.",
    "browse_empty_filtered": (
        "No profiles match your filters right now. Change or clear them with /filters, "
        "or check back later! 🔎"
    ),
    # --- P8 profile boost
    "boost.btn": "🚀 Activate Boost",
    "boost.intro": (
        "🚀 <b>Profile Boost</b>\n\nYour profile will receive higher visibility for a limited "
        "time: {minutes} minutes. After a boost starts, the next one can start {hours} hours "
        "later."
    ),
    "boost.active": (
        "🚀 <b>Boost active</b>\nExpires: {ends}\nTime left: {left} min\n"
        "Next boost possible after: {next}"
    ),
    "boost.cooldown": "⏳ Your last boost is over. You can boost again after {next}.",
    "boost.locked": "🔒 Profile Boost is available with Pro/VIP: /premium",
    # --- load testing: admin confirmation in Telegram
    "loadtest.confirm": (
        "🚨 <b>LOAD TEST CONFIRMATION</b>\n\n"
        "Environment: <b>TESTING</b>\n"
        "Chats: {chats}\n"
        "Messages / chat / min: {rate}\n"
        "Ramp-up: {ramp}\n"
        "Duration: {duration}\n"
        "Estimated messages: {estimated}\n\n"
        "This may consume significant server resources.\n"
        "Valid for {valid} minutes, works once."
    ),
    "loadtest.btn.confirm": "✅ CONFIRM LOAD TEST",
    "loadtest.btn.cancel": "❌ CANCEL",
    "loadtest.duration.minute": "1 minute",
    "loadtest.duration.minutes": "{n} minutes",
    "loadtest.duration.seconds": "{n} seconds",
    "loadtest.confirmed": (
        "✅ Confirmed. Load test #{run} is queued and the worker will start it shortly. "
        "You can stop it at any time in the admin panel."
    ),
    "loadtest.cancelled": "❌ Load test cancelled. Nothing was started.",
    "loadtest.expired": "⌛ This confirmation expired. Nothing was started.",
    "loadtest.invalid": "⚠️ This confirmation is no longer valid. Nothing was started.",
    "loadtest.blocked": "🚫 <b>LOAD TEST BLOCKED</b>\nProduction or unknown environment detected.",
    "loadtest.busy": "🚫 Another load test is already running.",
    "loadtest.denied": "⛔ Only administrators can answer a load test confirmation.",
}


def _build() -> dict[str, str]:
    catalog: dict[str, str] = {}
    for name, value in list(globals().items()):
        if not (name.isupper() and isinstance(value, str)) or name in _NOT_TRANSLATED:
            continue
        if name.startswith("ADMIN_") and name != "ADMIN_DENIED":
            continue  # operator-only panel text
        catalog[name.lower()] = value
    catalog.update({f"invalid.{k}": v for k, v in INVALID.items()})
    catalog.update({f"report_reason.{k}": v for k, v in REPORT_REASON_LABEL.items()})
    catalog.update(_EXTRA)
    return catalog


MESSAGES: dict[str, str] = _build()
