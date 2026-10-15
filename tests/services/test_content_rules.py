import pytest

from bot.services.content_rules import contact_violation, normalize, profile_text_violation


@pytest.mark.parametrize(
    "text",
    [
        "hello there!",
        "what's your favourite movie?",
        "I'm free at 5 pm, see you at 7.30",
        "version 1.2.3 is out",
        "I am 25 years old",
        "lol :) / ok",
        "3 + 4 = 7",
        "call it a day",
    ],
)
def test_harmless_chat_text_passes(text):
    assert contact_violation(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "visit http://evil.com",
        "HTTPS://EVIL.COM",
        "go to Evil.COM now",
        "www.site.org",
        " wwｗ.site",  # fullwidth letter
        "ｈｔｔｐｓ：／／ａ.ｂ",  # fullwidth scheme
        "hхtps://x",  # Cyrillic homoglyph, caught by '://'
        "t.me/somebody",
        "T . me / x",
        "telegram.me/x",
        "tg://user?id=1",
        "ftp://files",
    ],
)
def test_links_are_detected_even_when_obfuscated(text):
    assert contact_violation(text) == "link"


@pytest.mark.parametrize(
    "text",
    [
        "add me @bob_99",
        "hi​ @​someone",  # zero-width characters
        "mail a@b.co",
        "call 9876543210",
        "+91 98765-43210",
        "(555) 123-4567 890",
    ],
)
def test_handles_emails_and_phone_numbers_are_detected(text):
    assert contact_violation(text) == "contact"


@pytest.mark.parametrize(
    "etype", ["url", "text_link", "text_mention", "mention", "email", "phone_number"]
)
def test_telegram_entities_that_carry_links_are_rejected(etype):
    assert contact_violation("click here", (etype,)) == "link"


def test_harmless_entities_are_fine():
    assert contact_violation("bold text", ("bold", "italic", "spoiler")) is None


def test_normalize_strips_invisible_characters_and_folds_case():
    assert normalize("A​B⁠c") == "abc"
    assert normalize("ＡＢＣ") == "abc"


@pytest.mark.parametrize(
    "text",
    [
        "I'm 16 and love music",
        "im 15",
        "I am 17",
        "aged 17",
        "age: 14",
        "16 years old girl",
        "14 yo",
        "underage fun",
        "under 18",
        "schoolgirl",
        "loli",
    ],
)
def test_profile_text_mentioning_minors_is_rejected(text):
    assert profile_text_violation(text) == "minor"


@pytest.mark.parametrize(
    "text",
    [
        "Alice",
        "I love hiking and coffee ☕",
        "I am 18",
        "I am 25 and a nurse",
        "I'm 5'9 and tall",
        "I am 10 minutes away",
        "I am 6ft tall",
        "I have a 4 year plan",
        "Born in 1999",
    ],
)
def test_normal_profile_text_is_accepted(text):
    assert profile_text_violation(text) is None


@pytest.mark.parametrize(
    "text", ["looking for nudes", "sugar daddy wanted", "OnlyFans link", "sex chat?", "send pics"]
)
def test_sexual_solicitation_in_profiles_is_rejected(text):
    assert profile_text_violation(text) in ("sexual", "contact", "link")
    assert profile_text_violation(text) is not None


def test_profile_text_also_rejects_links_and_contacts():
    assert profile_text_violation("my site foo.com") == "link"
    assert profile_text_violation("insta @bob_99") == "contact"


def test_email_with_a_domain_is_rejected_as_link_or_contact():
    assert contact_violation("write to john.doe@example.com") in ("link", "contact")
