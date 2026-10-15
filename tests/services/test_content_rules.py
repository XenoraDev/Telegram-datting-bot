import pytest

from bot.services.content_rules import (
    NO_CARRY,
    chat_contact_violation,
    contact_violation,
    normalize,
    profile_text_violation,
)


def chat(text, carry=NO_CARRY):
    return chat_contact_violation(text, (), carry)


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


# --- chat filter (usernames allowed, contacts and links blocked) ---------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "9876543210",
        "98765 43210",
        "98765-43210",
        "+91 98765 43210",
        "+919876543210",
        "+91-98765-43210",
        "(987) 654-3210",
        "098765 43210",
        "9 8 7 6 5 4 3 2 1 0",
        "9.8.7.6.5.4.3.2.1.0",
        "9-8-7-6-5-4-3-2-1-0",
        "98765,43210",
        "nine eight seven six five four three two one zero",
        "9876 five 43210",
        "९८७६५४३२१०",  # Devanagari digits
        "٩٨٧٦٥٤٣٢١٠",  # Arabic-Indic digits
        "９８７６５４３２１０",  # fullwidth
        "9️⃣8️⃣7️⃣6️⃣5️⃣4️⃣3️⃣2️⃣1️⃣0️⃣",
        "9\u200b8\u200b7\u200b6\u200b5\u200b4\u200b3\u200b2\u200b1\u200b0",
        "+44 7911 123456",
        "+1 (555) 123-4567",
        "ek do teen char paanch chhe saat aath nau shunya",
        "call me on 9876543210 pls",
        "call 12345678",
        "whatsapp 1234567",
        "+9876 5432",
    ],
)
def test_chat_blocks_phone_numbers_in_many_formats(text):
    assert chat(text)[0] == "contact"


@pytest.mark.parametrize(
    "text",
    [
        "12345678",  # 8 digits are NOT automatically a phone number
        "123456789",
        "1234567890",  # 10 digits starting with 1 and no cue
        "i scored 85 92 in maths",
        "on 12/05/2024 at 10:30",
        "12-05-2024",
        "it costs 1,00,00,000",
        "population 8,000,000,000",
        "meet at 7.30 pm, 2 hours",
        "i am 25, 180 cm, 75 kg",
        "pin 560001",
        "call 100",
        "my number is 7",
        "version 1.2.3.4",
        "one two three four",
        "do you have ek dost",
    ],
)
def test_chat_keeps_ordinary_numbers_usable(text):
    assert chat(text)[0] is None


@pytest.mark.parametrize(
    "text",
    [
        "john@example.com",
        "john . doe@gmail.com",
        "john (at) gmail (dot) com",
        "john [at] gmail [dot] com",
        "john{at}gmail{dot}com",
        "john at gmail dot com",
        "john at gmail",
        "john @ gmail",
        "john @ gmail . com",
        "JOHN＠GMAIL．COM",
        "john​@​gmail.com",
    ],
)
def test_chat_blocks_plain_and_lightly_obfuscated_emails(text):
    assert chat(text)[0] in ("contact", "link")


@pytest.mark.parametrize(
    "text",
    [
        "http://evil.com",
        "https://evil.com/x",
        "HTTPS://EVIL.COM",
        "www.evil.org",
        "t.me/rahul123",
        "https://t.me/rahul123",
        "t.me",
        "T . me / rahul123",
        "telegram.me/rahul123",
        "telegram.dog/x",
        "tg://resolve?domain=x",
        "evil.com",
        "evil[.]com",
        "evil(.)com",
        "evil (dot) com",
        "evil dot com",
        "hxxps://evil.com",
        "ｈｔｔｐｓ：／／ｅｖｉｌ．ｃｏｍ",
    ],
)
def test_chat_blocks_links_and_telegram_links(text):
    assert chat(text)[0] == "link"


@pytest.mark.parametrize(
    "text",
    [
        "@rahul123",
        "add me @rahul123",
        "@Rahul_99 here",
        "my telegram is @a_b_c_d_e",
        "hey @bob",
        "meet @ 5.30 ok",
        "see you @ 7 pm",
        "polka dot in the sky",
    ],
)
def test_chat_allows_telegram_usernames_and_normal_at_signs(text):
    assert chat(text)[0] is None


def test_chat_allows_mention_entities_but_not_other_link_entities():
    assert chat_contact_violation("hi @rahul123", ("mention",))[0] is None
    for etype in ("url", "text_link", "text_mention", "email", "phone_number"):
        assert chat_contact_violation("hi", (etype,))[0] == "link"


def test_profile_text_stays_strict_about_usernames_and_digit_runs():
    assert contact_violation("add me @rahul123") == "contact"
    assert contact_violation("12345678") == "contact"
    assert contact_violation("nine eight seven six five four three two one zero") == "contact"
    assert contact_violation("john (at) gmail (dot) com") == "link"


def test_carry_joins_a_number_split_across_messages():
    bad, carry = chat("98765")
    assert bad is None and carry.digits == "98765"
    assert chat("43210", carry)[0] == "contact"
    assert chat("43210 bye", carry)[0] == "contact"
    assert chat("ok 43210", carry)[0] is None  # a later number, not a continuation
    assert chat("43210", NO_CARRY)[0] is None


def test_carry_keeps_only_digits_never_text():
    _, carry = chat("hello there, my pin is 1 2 3 4 5 6 7")
    assert carry.digits == "1234567"
    _, carry = chat("no digits here")
    assert carry.digits == ""


def test_carry_only_continues_from_a_run_at_the_end_of_the_message():
    assert chat("98765 and then some words")[1].digits == ""
    assert chat("cell 98765")[1].digits == "98765"


def test_cue_carries_to_the_next_message():
    _, carry = chat("here is my whatsapp number")
    assert carry.cue and chat("12345678", carry)[0] == "contact"
    assert chat("12345678")[0] is None
