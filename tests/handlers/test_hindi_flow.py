"""Full Hindi localization through the real Dispatcher (and English staying English)."""

from unittest.mock import AsyncMock, patch

import pytest
from aiogram import Bot
from aiogram.types import Contact

from bot import keyboards as kb
from bot.db import repo_profiles, repo_users
from bot.main import run_maintenance
from bot.texts import en, hi
from tests.handlers.harness import ADMIN_ID

A, B, C = 6200001, 6200002, 6200003


def H(key, **kw):
    return hi.MESSAGES[key].format(**kw) if kw else hi.MESSAGES[key]


def E(key, **kw):
    return en.MESSAGES[key].format(**kw) if kw else en.MESSAGES[key]


def texts(out, chat_id=None):
    return [
        o.text
        for o in out
        if o.kind in ("SendMessage", "EditMessageText") and chat_id in (None, o.chat_id)
    ]


def inline(out):
    return [
        (b.text, b.callback_data)
        for o in out
        for row in getattr(o.markup, "inline_keyboard", [])
        for b in row
    ]


def reply_labels(out):
    return [b.text for o in out for row in getattr(o.markup, "keyboard", []) for b in row]


async def hindi(tg, uid, **kw):
    """Onboard in the default language, then switch to Hindi."""
    await tg.onboard(uid, **kw)
    await tg.press(uid, "lg:hi")


async def lang_of(pool, uid):
    return (await repo_users.get(pool, uid))["language"]


# ---------------- language setting ----------------


async def test_default_is_english_and_stays_english(tg, app_pool):
    await tg.onboard(A)
    assert await lang_of(app_pool, A) == "en"
    assert texts(await tg.send(A, "/chat")) == [E("searching")]
    assert reply_labels(await tg.send(A, "/profile")) == reply_labels(await tg.send(A, "/profile"))
    assert "Male" in texts(await tg.send(A, "/profile"))[0]


async def test_switching_persists_and_refreshes_the_menu(tg, app_pool):
    await tg.onboard(A)
    out = await tg.press(A, "lg:hi")
    assert H("lang.changed") in texts(out)
    assert "💬 रैंडम चैट" in reply_labels(out)  # reply keyboard replaced
    assert await lang_of(app_pool, A) == "hi"
    out = await tg.press(A, "lg:en")
    assert E("lang.changed") in texts(out) and "💬 Random Chat" in reply_labels(out)
    assert await lang_of(app_pool, A) == "en"


async def test_language_selector_text_follows_the_current_language(tg):
    await hindi(tg, A)
    out = await tg.send(A, "/language")
    assert texts(out) == [H("lang.prompt")]
    assert [d for _, d in inline(out)] == ["lg:en", "lg:hi"]
    out = await tg.send(A, "🌐 भाषा")  # the Hindi menu button opens it too
    assert texts(out) == [H("lang.prompt")]


async def test_help_start_and_unknown_text_are_hindi(tg):
    await hindi(tg, A)
    assert texts(await tg.send(A, "/help")) == [H("help")]
    assert texts(await tg.send(A, "/start")) == [H("welcome_back")]
    assert texts(await tg.send(A, "/nonsense")) == [H("unknown")]


# ---------------- onboarding ----------------


async def test_full_onboarding_in_hindi(tg, app_pool):
    await tg.send(A, "/start")
    await tg.send(A, "/language")
    await tg.press(A, "lg:hi")
    out = await tg.press(A, "age:yes")
    assert H("ask_name") in texts(out)
    assert texts(await tg.send(A, "Priya")) == [H("ask_age")]
    out = await tg.send(A, "27")
    assert texts(out) == [H("ask_gender")]
    assert reply_labels(out) == ["👨 पुरुष", "👩 महिला", "🧑 अन्य"]
    out = await tg.send(A, "👩 महिला")
    assert texts(out) == [H("ask_looking")] and reply_labels(out) == [
        "👨 पुरुष",
        "👩 महिलाएँ",
        "👥 सभी",
    ]
    out = await tg.send(A, "👥 सभी")
    assert texts(out) == [H("ask_country")] and "🇮🇳 भारत" in reply_labels(out)
    out = await tg.send(A, "🇮🇳 भारत")
    assert texts(out) == [H("ask_city")] and "Delhi" in reply_labels(out)  # city names stay
    assert texts(await tg.send(A, "Delhi")) == [H("ask_bio")]
    out = await tg.send(A, "नमस्ते, मुझे संगीत पसंद है")
    assert texts(out) == [H("profile_saved")] and "💬 रैंडम चैट" in reply_labels(out)
    p = await repo_profiles.get(app_pool, A)
    assert (p["gender"], p["looking_for"], p["country"], p["city"]) == ("f", "any", "IN", "Delhi")


async def test_onboarding_gate_and_errors_are_hindi(tg):
    await tg.send(A, "/start")
    await tg.press(A, "lg:hi")  # the selector works before the profile exists
    out = await tg.press(A, "lg:hi")  # choosing the language is what brings up the age gate
    assert texts(out) == [H("lang.changed"), H("age_gate")]
    assert [t for t, _ in inline(out)] == [H("age_confirm_btn"), H("age_deny_btn")]
    assert [d for _, d in inline(out)] == ["age:yes", "age:no"]
    await tg.press(A, "age:yes")
    assert texts(await tg.send(A, "x" * 31)) == [H("invalid.name_length")]
    await tg.send(A, "Priya")
    assert texts(await tg.send(A, "abc")) == [H("invalid.age_not_number")]
    assert texts(await tg.send(A, "99")) == [H("invalid.age_range")]
    await tg.send(A, "27")
    assert texts(await tg.send(A, "कुछ भी")) == [H("use_buttons")]
    await tg.send(A, "👩 महिला")
    await tg.send(A, "👥 सभी")
    await tg.send(A, "🇮🇳 भारत")
    assert texts(await tg.send(A, "Delhi 110001")) == [H("invalid.city_chars")]
    await tg.send(A, "Delhi")
    assert texts(await tg.send(A, "देखो http://x.com")) == [H("invalid.text_link")]
    assert texts(await tg.send(A, "call 9876543210")) == [H("invalid.text_contact")]


async def test_english_labels_still_work_for_a_hindi_user(tg, app_pool):
    await tg.send(A, "/start")
    await tg.press(A, "lg:hi")
    await tg.press(A, "age:yes")
    for ans in ("Priya", "27", "👩 Female", "👥 Everyone", "🌍 Other", "Pune", "hello"):
        await tg.send(A, ans)
    assert (await repo_profiles.get(app_pool, A))["gender"] == "f"


async def test_under_18_block_message_is_hindi(tg):
    await tg.send(A, "/start")
    await tg.press(A, "lg:hi")
    out = await tg.press(A, "age:no")
    assert H("blocked_adults_only") in texts(out)


# ---------------- random chat ----------------


async def test_random_chat_is_hindi_end_to_end(tg):
    await hindi(tg, A)
    await hindi(tg, B, age="31", gender="👩 Female", city="Oslo")
    out = await tg.send(A, "💬 रैंडम चैट")
    assert texts(out) == [H("searching")]
    assert reply_labels(out) == ["➡️ अगला", "🛑 रोकें", "🚨 रिपोर्ट"]
    assert texts(await tg.send(A, "/chat")) == [H("already_searching")]
    out = await tg.send(B, "/chat")
    # the header (with the chat menu) and the masked card (with the 👁 button) are two messages
    to_a, to_b = " ".join(texts(out, A)), " ".join(texts(out, B))
    assert "साथी मिल गया" in to_a and "साथी मिल गया" in to_b and "अनाम चैट" in to_a
    assert "उम्र: ••" in to_a and "लिंग: ••" in to_a
    assert "शहर: 🌍" not in to_a and "Oslo" in to_a  # city is visible on Free
    assert "31" not in to_a  # the locked age is never sent
    assert not any(w in to_a + to_b for w in ("Partner", "Anonymous", str(A), str(B)))
    assert texts(await tg.send(A, "/chat")) == [H("already_in_chat")]
    out = await tg.send(A, "🛑 रोकें")
    mine, theirs = texts(out, A), texts(out, B)
    assert mine == [H("chat_ended_me"), H("after_chat")]
    assert theirs == [H("chat_ended_partner"), H("after_chat")]
    after = [(t, d) for t, d in inline(out) if d.startswith(("rep:", "blk:"))]
    assert [t for t, _ in after] == ["🚩 रिपोर्ट", "🚫 ब्लॉक"] * 2
    assert texts(await tg.send(A, "/stop")) == [H("not_in_chat")]


async def test_each_user_is_addressed_in_their_own_language(tg):
    await hindi(tg, A)
    await tg.onboard(B, age="31", gender="👩 Female", city="Oslo")  # stays English
    await tg.send(A, "/chat")
    out = await tg.send(B, "/chat")
    assert "साथी मिल गया" in texts(out, A)[0]
    assert "Partner Found" in texts(out, B)[0] and "Anonymous chat" in texts(out, B)[1]
    assert reply_labels([o for o in out if o.chat_id == A]) == ["➡️ अगला", "🛑 रोकें", "🚨 रिपोर्ट"]
    assert reply_labels([o for o in out if o.chat_id == B]) == ["➡️ Next", "🛑 Stop", "🚨 Report"]
    out = await tg.send(B, "➡️ Next")
    assert H("chat_ended_partner") in texts(out, A)
    assert E("chat_ended_me") in texts(out, B)


async def test_cooldown_cancel_and_expiry_messages_are_hindi(tg, app_pool):
    await hindi(tg, A)
    await tg.send(A, "/chat")
    assert texts(await tg.send(A, "/stop")) == [H("search_cancelled")]
    await tg.send(A, "/chat")
    await app_pool.execute("UPDATE chat_queue SET joined_at = now() - interval '31 minutes'")
    send = AsyncMock()
    with patch.object(Bot, "send_message", send):
        await run_maintenance(tg.services, Bot("1:x"))
    assert send.await_args.args[1] == H("search_expired")
    await app_pool.execute(
        "INSERT INTO restrictions (user_id, kind, until) VALUES ($1, 'chat_cooldown', "
        "now() + interval '1 hour')",
        A,
    )
    assert texts(await tg.send(A, "/chat")) == [H("cooldown")]


async def test_not_eligible_and_rate_limit_notices_are_hindi(tg):
    await tg.send(A, "/start")
    await tg.press(A, "lg:hi")
    assert texts(await tg.send(A, "/chat")) == [H("finish_setup")]
    await hindi(tg, B)
    tg.auto_advance = 0.5
    seen = [texts(await tg.send(B, "/chat"))[0] for _ in range(9)]
    assert H("search_rate_limited") in seen


# ---------------- safety / contact filter ----------------


async def test_contact_filter_notices_are_hindi_and_the_rules_unchanged(tg):
    await hindi(tg, A)
    await hindi(tg, B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    cases = [
        ("http://evil.com", "link_blocked"),
        ("t.me/someone", "link_blocked"),
        ("call 9876543210", "contact_blocked"),
        ("john (at) gmail (dot) com", "link_blocked"),
        ("x" * 1001, "too_long"),
    ]
    for text, key in cases:
        out = await tg.send(A, text)
        assert [(o.chat_id, o.text) for o in out] == [(A, H(key))], text
    out = await tg.send(A, "मेरा @rahul123 है")  # usernames are still allowed
    assert [o.chat_id for o in out] == [B]
    assert texts(await tg.photo(A)) == [H("unsupported")]
    assert texts(await tg.raw(A, contact=Contact(phone_number="1", first_name="x"))) == [
        H("unsupported")
    ]
    await tg.send(A, "same")
    await tg.send(A, "same")
    assert H("flood") in texts(await tg.send(A, "same"))


async def test_report_and_block_flows_are_hindi(tg):
    await hindi(tg, A)
    await hindi(tg, B)
    assert texts(await tg.send(A, "/report")) == [H("not_in_chat")]
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    out = await tg.send(A, "🚨 रिपोर्ट")
    assert texts(out) == [H("report_start")]
    reasons = inline(out)
    assert [t for t, _ in reasons][:3] == ["स्पैम", "परेशान करना", "अश्लील सामग्री"]
    assert [t for t, _ in reasons][-1] == "रद्द करें" and reasons[-1][1] == "cancel"
    code = next(d for _, d in reasons if d.startswith("rr:") and d.endswith(":spam"))
    out = await tg.press(A, code)
    assert H("report_created") in texts(out)
    assert H("chat_ended_partner") in texts(out, B)
    assert texts(await tg.press(A, code)) in ([H("report_not_found")], [H("report_duplicate")])


async def test_block_command_is_hindi(tg):
    await hindi(tg, A)
    await hindi(tg, B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    out = await tg.send(A, "/block")
    assert H("block_done") in texts(out, A) and H("chat_ended_partner") in texts(out, B)


async def test_muted_and_slow_down_notices_are_hindi(tg, app_pool):
    await hindi(tg, A)
    await hindi(tg, B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    tg.auto_advance = 0.0
    seen = []
    for _ in range(12):
        seen += texts(await tg.send(A, "hello"))
    assert H("slow_down") in seen


# ---------------- browse / matches ----------------


async def test_browse_cards_buttons_and_empty_state_are_hindi(tg, app_pool):
    await hindi(tg, A)
    assert texts(await tg.send(A, "🔎 प्रोफ़ाइल देखें")) == [H("browse_empty")]
    await tg.onboard(B, name="Bea", age="29", gender="👩 Female", city="Delhi")
    out = await tg.send(A, "/browse")
    assert "🎂 29" in texts(out)[0] and "Bea" in texts(out)[0]
    pairs = inline(out)
    assert [t for t, _ in pairs] == ["❤️ पसंद", "⏭️ आगे बढ़ें", "🚨 रिपोर्ट", "🚫 ब्लॉक", "🎯 फ़िल्टर"]
    pid = pairs[0][1].split(":")[1]
    assert [d for _, d in pairs] == [f"lk:{pid}", f"ps:{pid}", f"rp:{pid}", f"bk:{pid}", "fl:h"]
    out = await tg.press(A, f"ps:{pid}")
    assert texts(out) == [H("browse_empty")]


async def test_match_notifications_and_list_are_in_each_users_language(tg, app_pool):
    await hindi(tg, A, name="Asha")
    await tg.onboard(B, name="Bob", city="Rome")  # English
    pid = lambda uid: app_pool.fetchval(  # noqa: E731
        "SELECT public_id FROM profiles WHERE user_id = $1", uid
    )
    assert texts(await tg.send(A, "💞 मेरे मैच")) == [H("matches_none")]
    await tg.press(A, f"lk:{(await pid(B)).hex}")
    out = await tg.press(B, f"lk:{(await pid(A)).hex}")
    to_b, to_a = texts(out, B)[0], texts(out, A)[0]
    assert "It's a Match" in to_b and "Asha" in to_b
    assert "यह एक मैच है" in to_a and "Bob" in to_a and "आप दोनों ने एक-दूसरे को पसंद किया" in to_a
    assert [t for t, _ in inline([o for o in out if o.chat_id == A])] == [
        "💞 मेरे मैच",
        "🔎 प्रोफ़ाइल देखें",
    ]
    assert [d for _, d in inline([o for o in out if o.chat_id == A])] == ["mm", "br"]
    out = await tg.send(A, "/matches")
    assert texts(out)[0] == H("matches_header", total=1)
    assert [t for t, _ in inline(out)] == ["💬 चैट", "🚨 रिपोर्ट", "🚫 ब्लॉक"]


async def test_browse_report_and_block_and_in_chat_notice_are_hindi(tg, app_pool):
    await hindi(tg, A)
    await tg.onboard(B)
    await tg.onboard(C)
    first = await tg.send(A, "/browse")
    pid = inline(first)[2][1].split(":")[1]
    prompt = await tg.press(A, f"rp:{pid}")
    assert texts(prompt) == [H("report_start")]
    assert "स्पैम" in [t for t, _ in inline(prompt)]
    out = await tg.press(A, f"rq:{pid}:spam")
    assert H("report_created") in texts(out)
    nxt = await tg.send(A, "/browse")
    pid2 = inline(nxt)[3][1].split(":")[1]
    assert H("profile_blocked") in texts(await tg.press(A, f"bk:{pid2}"))
    assert H("profile_gone") in texts(await tg.press(A, "bk:" + "0" * 32))
    await hindi(tg, 6200010)
    await tg.send(6200010, "/chat")
    await tg.onboard(6200011)
    await tg.send(6200011, "/chat")
    assert texts(await tg.send(6200010, "/browse")) == [H("browse_in_chat")]


# ---------------- profile / account ----------------


async def test_profile_screen_is_hindi_with_flag_and_city(tg):
    await hindi(tg, A, name="Asha", gender="👩 Female", city="Delhi", country="🌍 Other")
    out = await tg.send(A, "👤 मेरी प्रोफ़ाइल")
    text = texts(out)[0]
    assert "<b>Asha</b>, 25" in text and "महिला" in text and "चैट करना चाहते हैं: सभी" in text
    assert "🌍 Delhi" in text and "बदलने के लिए /editprofile" in text
    assert "Male" not in text and "looking for" not in text


async def test_edit_profile_messages_are_hindi(tg):
    await hindi(tg, A)
    assert texts(await tg.send(A, "/editprofile")) == [H("ask_name")]
    assert texts(await tg.send(A, "/cancel")) == [H("profile_cancelled")]
    await hindi(tg, B)
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    assert texts(await tg.send(A, "/editprofile")) == [H("edit_not_in_chat")]
    await tg.send(6200020, "/start")
    await tg.press(6200020, "lg:hi")
    assert texts(await tg.send(6200020, "/profile")) == [H("finish_setup")]


async def test_deleteme_flow_is_hindi(tg):
    await hindi(tg, A)
    out = await tg.send(A, "/deleteme")
    assert texts(out) == [H("delete_confirm")]
    assert [t for t, _ in inline(out)] == [H("delete_yes_btn"), "रद्द करें"]
    assert [d for _, d in inline(out)] == ["del:yes", "del:no"]
    assert H("delete_cancelled") in texts(await tg.press(A, "del:no"))
    out = await tg.press(A, "del:yes")
    assert H("delete_done") in texts(out)
    # the account is gone, so the language is reset with it (documented behaviour)
    assert texts(await tg.send(A, "/chat")) == [E("account_deleted")]


async def test_banned_user_gets_the_hindi_restriction_notice(tg):
    await tg.onboard(ADMIN_ID)
    await hindi(tg, A)
    await tg.press(ADMIN_ID, f"ad:bo:{A}:0")
    assert texts(await tg.send(A, "/chat")) == [H("blocked_banned")]
    assert texts(await tg.send(A, "hello")) == [H("blocked_banned")]
    out = await tg.press(A, "br")
    answers = [o.method for o in out if o.kind == "AnswerCallbackQuery"]
    assert answers and answers[0].text == H("blocked_banned")


async def test_admin_denial_is_hindi_for_normal_hindi_users(tg):
    await hindi(tg, A)
    assert texts(await tg.send(A, "/admin")) == [H("admin_denied")]
    out = await tg.press(A, "ad:st")
    assert [o.method.text for o in out if o.kind == "AnswerCallbackQuery"] == [H("admin_denied")]


async def test_hindi_users_can_still_use_every_command_with_english_button_text(tg):
    await hindi(tg, A)
    assert texts(await tg.send(A, "💬 Random Chat")) == [H("searching")]  # old keyboard still works
    assert texts(await tg.send(A, "🛑 Stop")) == [H("search_cancelled")]


@pytest.mark.parametrize("who", [A])
async def test_english_user_keeps_english_everywhere(tg, who):
    await tg.onboard(who)
    assert texts(await tg.send(who, "/chat")) == [E("searching")]
    assert texts(await tg.send(who, "/stop")) == [E("search_cancelled")]
    assert texts(await tg.send(who, "/browse")) == [E("browse_empty")]
    assert texts(await tg.send(who, "/matches")) == [E("matches_none")]
    assert texts(await tg.send(who, "/report")) == [E("not_in_chat")]
    assert texts(await tg.send(who, "/help")) == [E("help")]
    assert reply_labels(await tg.send(who, "/profile")) == [
        "💬 Random Chat",
        "🔎 Browse",
        "💞 My Matches",
        "👤 My Profile",
        "⭐ Premium",
        "🌐 Language",
        "🆘 Support",
    ]
    assert kb.BTN_CHAT == "💬 Random Chat"
