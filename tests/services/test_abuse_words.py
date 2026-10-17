"""The abusive-language filter: listed words and their disguises are caught; ordinary words that
merely contain a listed one are not."""

import pytest

from bot.services.abuse_words import has_abuse
from bot.services.chat_relay import Incoming
from tests.factories import make_user, start_chat

CAUGHT = [
    "fuck you", "You are a BITCH", "FUCK", "fuuuuck off", "what the fuuuck", "sh1t", "$hit happens",
    "f@ck", "f u c k", "f.u.c.k", "f-u-c-k you", "motherfucker", "fucking idiot", "bitches",
    "shitty day", "asshole", "a$$hole", "retard", "nigga", "bastard", "slut", "whore",
    "madarchod", "MADARCHOD!!", "maderchod", "behenchod", "bhenchod", "bhosdike", "bsdk",
    "tu chutiya hai", "chutiye", "chut", "randi", "harami", "haramkhor", "gandu", "gaand",
    "b h o s d i k e", "m.a.d.a.r.c.h.o.d", "tum kamine ho",
    "मादरचोद", "बहनचोद", "भोसड़ीके", "चूतिया", "तू गांडू है", "तेरी रंडी", "हरामी कहीं का",
    "ok.\n\nfuck", "hello, bitch!", "(shit)", "“fuck”", "fu​ck",  # zero-width space inside
]  # fmt: skip

CLEAN = [
    "class", "assistant", "Scunthorpe", "chutney", "shiitake", "bass", "passenger", "analysis",
    "assume", "assess", "classic", "glass", "grass", "mass", "pass", "compass", "hello there",
    "how are you?", "mera naam Amit hai", "kutta pyara hai", "chod do yaar", "mc", "bc", "push",
    "hit me up", "sit here", "bitcoin", "night", "cockpit", "Dickens", "document", "shut up",
    "I love chutney and pass the salt", "Hancock", "Essex", "title", "grape",
    "मेरा नाम अमित है", "चटनी", "कुत्ता", "नमस्ते दोस्त", "मैं ठीक हूँ", "लंदन", "चोर",
    "I am 25 and I like movies", "call me at the cafe", "5 pm?", "see you at 7", "ok", "",
    "?!", "   ", "a b c", "i a m a b o y",
]  # fmt: skip


@pytest.mark.parametrize("text", CAUGHT)
def test_abusive_words_and_their_disguises_are_caught(text):
    assert has_abuse(text), text


@pytest.mark.parametrize("text", CLEAN)
def test_ordinary_words_that_only_contain_a_listed_word_are_not_touched(text):
    assert not has_abuse(text), text


def test_a_long_innocent_text_is_fast_and_clean():
    text = "hello friend how was your day, we went to the class and had chutney " * 40
    assert not has_abuse(text[:1000])


# ------------------------------------------------------------------ chat relay


async def test_an_abusive_message_is_not_delivered_buffered_or_stored(app_pool, svc):
    for uid in (1, 2):
        await make_user(app_pool, uid)
    await start_chat(svc, 1, 2)
    res = await svc.relay.relay(1, Incoming("text", text="you are a chutiya"))
    assert res.status == "abuse" and res.text == "" and res.partner_id is None
    ok = await svc.relay.relay(1, Incoming("text", text="sorry, hello"))
    assert ok.status == "ok" and ok.text == "sorry, hello"  # the chat carries on normally
    assert "chutiya" not in repr(svc.buffer.__dict__)  # nothing of it was buffered
    for table in await app_pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'"):
        query = f'SELECT row_to_json(x)::text AS j FROM "{table["tablename"]}" x'  # noqa: S608
        rows = await app_pool.fetch(query)
        assert not any("chutiya" in r["j"] for r in rows)


async def test_a_clean_message_with_a_listed_word_inside_another_still_goes_through(app_pool, svc):
    for uid in (1, 2):
        await make_user(app_pool, uid)
    await start_chat(svc, 1, 2)
    res = await svc.relay.relay(1, Incoming("text", text="the assistant likes chutney"))
    assert res.status == "ok"


async def test_a_blocked_message_does_not_disturb_the_split_number_check(app_pool, svc):
    for uid in (1, 2):
        await make_user(app_pool, uid)
    await start_chat(svc, 1, 2)
    assert (await svc.relay.relay(1, Incoming("text", text="call me 98765"))).status == "ok"
    assert (await svc.relay.relay(1, Incoming("text", text="fuck"))).status == "abuse"
    assert (await svc.relay.relay(1, Incoming("text", text="43210"))).status == "contact"
