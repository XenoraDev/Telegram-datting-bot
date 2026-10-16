"""Translation catalogs: complete, consistent, safe fallback, language-independent callbacks."""

import re
from pathlib import Path
from string import Formatter

import pytest

from bot import keyboards as kb
from bot.texts import en, hi, i18n
from bot.texts.i18n import tr

PID = "a" * 32
# proper nouns and payment / file terms that stay as they are
HINDI_OK_LATIN = {
    "Delhi",
    "English",
    "Telegram",
    "UPI",
    "UTR",
    "QR",
    "ID",
    "MB",
    "JPG",
    "PNG",
    "WebP",
    "UTC",
}


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


def tags(text: str) -> list[str]:
    return sorted(re.findall(r"</?[a-z]+>", text))


def test_hindi_has_exactly_the_english_keys():
    assert set(hi.MESSAGES) == set(en.MESSAGES)
    assert len(en.MESSAGES) == len(hi.MESSAGES) >= 120


@pytest.mark.parametrize("key", sorted(en.MESSAGES))
def test_every_hindi_message_is_filled_and_keeps_placeholders_and_html(key):
    e, h = en.MESSAGES[key], hi.MESSAGES[key]
    assert h.strip()
    assert placeholders(h) == placeholders(e), key
    assert tags(h) == tags(e), key
    # slash-commands must stay untouched so they keep working
    assert set(re.findall(r"/[a-z]+", h)) == set(re.findall(r"/[a-z]+", e)), key


@pytest.mark.parametrize("key", sorted(en.MESSAGES))
def test_hindi_messages_are_really_hindi(key):
    text = hi.MESSAGES[key]
    stripped = re.sub(r"<[^>]+>|\{[^}]*\}|/[a-z]+", " ", text)
    latin = {w for w in re.findall(r"[A-Za-z]{2,}", stripped)} - HINDI_OK_LATIN
    assert not latin, f"{key}: untranslated words {latin}"
    if key not in {"card", "match_card", "pc.rule"}:  # pure templates of placeholders + emoji
        assert re.search(r"[ऀ-ॿ]", text), key


def test_no_two_keys_carry_the_same_english_sentence_by_accident():
    # duplicates are allowed only where the UX needs the same label in two places
    allowed = {
        "Cancel",
        "🚨 Report",
        "💞 My Matches",
        "🔎 Browse",
        "Other",
        "Please choose one of the buttons.",  # two distinct validation codes, one wording
        "⭐ Premium",  # the menu button and the partner-card badge are the same label
    }
    seen: dict[str, str] = {}
    for key, text in en.MESSAGES.items():
        if text in seen and text not in allowed:
            pytest.fail(f"{key} duplicates {seen[text]}: {text!r}")
        seen.setdefault(text, key)


def test_every_key_used_in_code_exists_in_the_catalog():
    """Static scan: each literal tr("...") in bot/ must be a real key."""
    used = set()
    for path in Path("bot").rglob("*.py"):
        used |= set(re.findall(r'\btr\(\s*"([a-z_.]+)"', path.read_text(encoding="utf-8")))
    assert used, "scan found nothing: the pattern is broken"
    assert used <= set(en.MESSAGES), sorted(used - set(en.MESSAGES))


def test_dynamic_keys_resolve_in_both_languages():
    for lang in i18n.SUPPORTED:
        for code in ("m", "f", "o"):
            assert tr(f"gender.{code}", lang)
        for code in ("m", "f", "any"):
            assert tr(f"looking.{code}", lang)
        for code in kb.COUNTRY_BUTTONS.values():
            assert tr(f"country.{code}", lang)
        for r in ("spam", "harassment", "sexual", "minor", "scam", "other"):
            assert tr(f"report_reason.{r}", lang)
        for code in en.INVALID:
            assert tr(f"invalid.{code}", lang)


# ---------------- fallback ----------------


def test_missing_hindi_key_falls_back_to_english_and_never_crashes(monkeypatch):
    monkeypatch.delitem(hi.MESSAGES, "searching")
    assert tr("searching", "hi") == en.MESSAGES["searching"]
    assert tr("searching", "xx") == en.MESSAGES["searching"]
    assert tr("searching", None) == en.MESSAGES["searching"]


def test_unknown_or_garbage_language_values_use_english():
    for bad in (None, "", "fr", "HI", "hi ", 5, "../etc"):
        assert i18n.normalize(bad) == "en"


# ---------------- keyboards ----------------


def _inline_pairs(markup):
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _reply_texts(markup):
    return [b.text for row in markup.keyboard for b in row]


def test_hindi_reply_keyboards_show_hindi_labels():
    assert _reply_texts(kb.main_menu("hi")) == [
        "💬 रैंडम चैट",
        "🔎 प्रोफ़ाइल देखें",
        "💞 मेरे मैच",
        "👤 मेरी प्रोफ़ाइल",
        "⭐ प्रीमियम",
        "🌐 भाषा",
    ]
    assert _reply_texts(kb.chat_menu("hi")) == ["➡️ अगला", "🛑 रोकें", "🚨 रिपोर्ट"]
    assert _reply_texts(kb.gender_kb("hi")) == ["👨 पुरुष", "👩 महिला", "🧑 अन्य"]
    assert _reply_texts(kb.looking_kb("hi")) == ["👨 पुरुष", "👩 महिलाएँ", "👥 सभी"]
    assert "🇮🇳 भारत" in _reply_texts(kb.country_kb("hi"))


def test_english_keyboards_are_unchanged():
    assert _reply_texts(kb.main_menu("en")) == [
        "💬 Random Chat",
        "🔎 Browse",
        "💞 My Matches",
        "👤 My Profile",
        "⭐ Premium",
        "🌐 Language",
    ]
    assert _reply_texts(kb.chat_menu("en")) == ["➡️ Next", "🛑 Stop", "🚨 Report"]
    assert _reply_texts(kb.gender_kb("en")) == ["👨 Male", "👩 Female", "🧑 Other"]
    assert _reply_texts(kb.looking_kb("en")) == ["👨 Men", "👩 Women", "👥 Everyone"]


def test_hindi_inline_buttons_use_the_agreed_labels():
    texts = [t for t, _ in _inline_pairs(kb.browse_card(PID, "hi"))]
    assert texts == ["❤️ पसंद", "⏭️ आगे बढ़ें", "🚨 रिपोर्ट", "🚫 ब्लॉक", "🎯 फ़िल्टर"]
    assert [t for t, _ in _inline_pairs(kb.match_actions("hi"))] == ["💞 मेरे मैच", "🔎 प्रोफ़ाइल देखें"]


BUILDERS = {
    "age_gate": lambda lg: kb.age_gate(lg),
    "delete_confirm": lambda lg: kb.delete_confirm(lg),
    "after_chat": lambda lg: kb.after_chat(7, lg),
    "report_reasons": lambda lg: kb.report_reasons(7, lg),
    "browse_card": lambda lg: kb.browse_card(PID, lg),
    "match_card": lambda lg: kb.match_card(PID, lg),
    "match_actions": lambda lg: kb.match_actions(lg),
    "profile_report_reasons": lambda lg: kb.profile_report_reasons(PID, "rq", lg),
}


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_callback_data_is_identical_in_every_language(name):
    en_pairs = _inline_pairs(BUILDERS[name]("en"))
    hi_pairs = _inline_pairs(BUILDERS[name]("hi"))
    assert [d for _, d in en_pairs] == [d for _, d in hi_pairs]
    assert [t for t, _ in en_pairs] != [t for t, _ in hi_pairs]  # only the display text differs
    for _, data in hi_pairs:  # and no callback ever contains Devanagari / translated text
        assert data.isascii() and len(data.encode()) <= 64


def test_reply_button_labels_are_recognised_in_every_language():
    assert kb.labels("btn.chat") == {"💬 Random Chat", "💬 रैंडम चैट"}
    assert kb.GENDER_LABELS["👩 महिला"] == "f" and kb.GENDER_LABELS["👩 Female"] == "f"
    assert kb.LOOKING_LABELS["👥 सभी"] == "any" and kb.COUNTRY_LABELS["🇮🇳 भारत"] == "IN"


def test_premium_callback_data_is_identical_in_every_language_and_strictly_parsed():
    plans = [(3, "Premium", "99", 30)]
    for lang in ("en", "hi"):
        assert [d for _, d in _inline_pairs(kb.premium_plans(plans, lang))] == ["pm:s:3"]
        assert [d for _, d in _inline_pairs(kb.payment_actions(5, lang))] == ["pm:u:5", "pm:c:5"]
        assert [d for _, d in _inline_pairs(kb.skip_utr(5, lang))] == ["pm:k:5"]
    assert kb.parse_premium_cb("pm:u:12") == ("u", 12)
    for bad in (None, "", "pm:x:1", "pm:u:", "pm:u:-1", "pm:u:1:2", "PM:u:1", "pm:u:" + "9" * 13):
        assert kb.parse_premium_cb(bad) is None


def test_every_premium_text_exists_in_both_languages_with_the_same_placeholders():
    keys = [k for k in en.MESSAGES if k.startswith(("premium.", "btn.premium", "btn.plan"))]
    assert len(keys) >= 25
    for key in keys:
        assert placeholders(en.MESSAGES[key]) == placeholders(hi.MESSAGES[key]), key


def test_the_tier_partner_card_privacy_limit_and_likes_texts_exist_in_both_languages():
    prefixes = ("pc.", "privacy.", "limit.", "likes.", "premium.queued", "browse_rate_limited")
    keys = [k for k in en.MESSAGES if k.startswith(prefixes)]
    assert len(keys) >= 30
    for key in keys:
        assert key in hi.MESSAGES, key
        assert placeholders(en.MESSAGES[key]) == placeholders(hi.MESSAGES[key]), key
        assert tags(en.MESSAGES[key]) == tags(hi.MESSAGES[key]), key
    # the lock placeholder and the badges differ per language but are never empty
    for lang in ("en", "hi"):
        for key in ("pc.locked", "pc.hidden", "pc.badge.1", "pc.badge.2", "pc.badge.3", "pc.cta"):
            assert tr(key, lang).strip()
    assert tr("pc.locked", "en") == "🔒 Premium" and tr("pc.locked", "hi") == "🔒 प्रीमियम"


def test_privacy_keyboard_callbacks_are_identical_in_every_language_and_strictly_parsed():
    settings = {"age": True, "gender": False, "city": True}
    for lang in ("en", "hi"):
        assert [d for _, d in _inline_pairs(kb.privacy_kb(settings, lang))] == [
            "pv:age",
            "pv:gender",
            "pv:city",
        ]
    assert kb.parse_privacy_cb("pv:age") == "age"
    for bad in (None, "", "pv:", "pv:name", "pv:age:1", "PV:age", "pv:age ", "x pv:age"):
        assert kb.parse_privacy_cb(bad) is None


P8_PREFIXES = ("filters.", "boost.")
P8_KEYS = ("browse_empty_filtered", "invalid.filter_age", "invalid.filter_choice")


def _p8_keys():
    from bot.texts import en

    return sorted(k for k in en.MESSAGES if k.startswith(P8_PREFIXES) or k in P8_KEYS)


def test_every_advanced_filter_and_boost_text_exists_in_both_languages():
    from bot.texts import en, hi

    keys = _p8_keys()
    assert len(keys) >= 30
    assert [k for k in keys if k not in hi.MESSAGES] == []
    for k in keys:
        assert hi.MESSAGES[k] != en.MESSAGES[k], k  # actually translated
        assert any("ऀ" <= c <= "ॿ" for c in hi.MESSAGES[k]), k  # in Devanagari


def test_advanced_filter_and_boost_texts_keep_the_same_placeholders():
    import string

    from bot.texts import en, hi

    def fields(text):
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    for k in _p8_keys():
        assert fields(en.MESSAGES[k]) == fields(hi.MESSAGES[k]), k


def test_filter_and_boost_buttons_are_translated_and_keep_ascii_callbacks():
    for lang in ("en", "hi"):
        for markup in (
            kb.filters_home(lang),
            kb.filters_age(lang),
            kb.filters_gender(lang),
            kb.filters_looking(lang),
            kb.filters_city(lang),
            kb.boost_kb(lang),
        ):
            for _, data in _inline_pairs(markup):
                assert data.isascii() and len(data.encode()) <= 64
    assert [t for t, _ in _inline_pairs(kb.boost_kb("hi"))] == ["🚀 बूस्ट चालू करें"]
    assert [d for _, d in _inline_pairs(kb.filters_home("en"))] == [
        d for _, d in _inline_pairs(kb.filters_home("hi"))
    ]
