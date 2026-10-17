"""Random chat through Telegram: matching messages, relay rules, report/block buttons."""

import pytest
from aiogram.types import Contact, Dice, Document, Location, MessageEntity, Voice

from bot import keyboards as kb
from bot.db import repo_chat, repo_safety
from bot.texts import en as t
from tests.handlers.harness import FakeTelegram

A, B = 4242001, 4242002  # distinctive ids: easy to prove they never leak into payloads


async def paired(tg: FakeTelegram, a=A, b=B, **kw):
    await tg.onboard(a, **kw.get("a", {}))
    await tg.onboard(b, **kw.get("b", {}))
    await tg.send(a, "/chat")
    await tg.send(b, "/chat")
    sid = await tg.services.matchmaking.current_chat(a)
    assert sid is not None
    return sid[0]


# ---------------- matching ----------------


async def test_search_then_match_shows_the_anonymous_card_with_protected_fields_locked(tg):
    await tg.onboard(
        A, age="31", gender="👩 Female", city="Oslo", name="Secret Name", bio="secret bio"
    )
    await tg.onboard(B, age="28", gender="👨 Male", city="Rome")
    out = await tg.send(A, "/chat")
    assert out[0].text == t.SEARCHING
    await tg.send(B, "/chat")
    to_a = tg.texts(A)[-1]
    to_b = tg.texts(B)[-1]
    assert "Partner Found" in tg.texts(A)[-2] and "Rome" in to_a and "Anonymous chat" in to_a
    assert "Oslo" in to_b
    # a Free viewer sees every field, but age and gender are locked: the values are never sent
    assert "Age: ••" in to_a and "Gender: ••" in to_a
    assert "Age: ••" in to_b and "Gender: ••" in to_b
    for forbidden in ("28", "31", "Male", "Female", "👨", "👩"):
        assert forbidden not in to_a + to_b
    for forbidden in ("Secret Name", "secret bio", "user4242001", "user4242002", f"{A}", f"{B}"):
        assert forbidden not in to_a + to_b


async def test_partner_info_is_html_escaped(tg):
    await tg.onboard(A)
    await tg.onboard(B, city="<b>Rome</b> & co")
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    assert "&lt;b&gt;Rome&lt;/b&gt; &amp; co" in tg.texts(A)[-1]
    assert "<b>Rome</b>" not in tg.texts(A)[-1]


async def test_menu_button_starts_a_search_too(tg):
    await tg.onboard(A)
    assert (await tg.send(A, kb.BTN_CHAT))[0].text == t.SEARCHING


async def test_repeated_search_and_already_in_chat_messages(tg):
    await tg.onboard(A)
    await tg.send(A, "/chat")
    assert (await tg.send(A, "/chat"))[0].text == t.ALREADY_SEARCHING
    await tg.onboard(B)
    await tg.send(B, "/chat")
    assert (await tg.send(A, "/chat"))[0].text == t.ALREADY_IN_CHAT


async def test_stop_cancels_a_search(tg):
    await tg.onboard(A)
    await tg.send(A, "/chat")
    assert (await tg.send(A, "/stop"))[0].text == t.SEARCH_CANCELLED
    assert (await tg.send(A, "/stop"))[0].text == t.NOT_IN_CHAT


async def test_search_spam_is_rate_limited(tg):
    await tg.onboard(A)
    tg.auto_advance = 0.5
    texts = [(await tg.send(A, "/chat"))[0].text for _ in range(9)]
    assert t.SEARCH_RATE_LIMITED in texts


# ---------------- relay ----------------


async def test_text_reaches_only_the_partner_as_plain_protected_text(tg):
    await paired(tg)
    n_before = len(tg.inbox(A))
    out = await tg.send(A, "hello <b>there</b> & bye")
    sent = [o for o in out if o.kind == "SendMessage"]
    assert len(sent) == 1 and sent[0].chat_id == B
    m = sent[0].method
    assert m.text == "hello <b>there</b> & bye"
    assert m.parse_mode is None  # never interpreted as HTML
    assert m.protect_content is True
    assert m.link_preview_options and m.link_preview_options.is_disabled
    assert len(tg.inbox(A)) == n_before  # no echo to the sender


async def test_stickers_are_forwarded_by_file_id_only(tg):
    await paired(tg)
    out = await tg.sticker(A, "STK42")
    sent = [o for o in out if o.kind == "SendSticker"]
    assert len(sent) == 1 and sent[0].chat_id == B and sent[0].method.sticker == "STK42"
    assert sent[0].method.protect_content is True


async def test_links_emails_and_phones_are_blocked_with_a_notice(tg):
    await paired(tg)
    for text, notice in [
        ("http://evil.com", t.LINK_BLOCKED),
        ("visit evil.com", t.LINK_BLOCKED),
        ("t.me/someone", t.LINK_BLOCKED),
        ("call +49 170 1234567", t.CONTACT_BLOCKED),
        ("98765 43210", t.CONTACT_BLOCKED),
        ("mail me: john (at) gmail (dot) com", t.LINK_BLOCKED),
    ]:
        out = await tg.send(A, text)
        assert [(o.chat_id, o.text) for o in out] == [(A, notice)], text


async def test_telegram_usernames_pass_through_in_chat(tg):
    await paired(tg)
    out = await tg.send(A, "add @someone_x")
    assert [(o.chat_id, o.text) for o in out] == [(B, "add @someone_x")]
    out = await tg.send(A, "hi", entities=[MessageEntity(type="mention", offset=0, length=2)])
    assert [o.chat_id for o in out] == [B]


async def test_split_phone_number_is_blocked_at_the_second_part(tg):
    await paired(tg)
    assert [o.chat_id for o in await tg.send(A, "98765")] == [B]
    out = await tg.send(A, "43210")
    assert [(o.chat_id, o.text) for o in out] == [(A, t.CONTACT_BLOCKED)]


async def test_hidden_link_entities_are_blocked_even_when_text_looks_clean(tg):
    await paired(tg)
    for etype in ("text_link", "url", "text_mention", "email", "phone_number"):
        out = await tg.send(A, "click me", entities=[MessageEntity(type=etype, offset=0, length=5)])
        assert [o.chat_id for o in out] == [A], etype
        assert out[0].text == t.LINK_BLOCKED


@pytest.mark.parametrize(
    "fields",
    [
        {"voice": Voice(file_id="v", file_unique_id="uv", duration=1)},
        {"document": Document(file_id="d", file_unique_id="ud")},
        {"location": Location(latitude=1.0, longitude=2.0)},
        {"contact": Contact(phone_number="123", first_name="x")},
        {"dice": Dice(emoji="🎲", value=3)},
    ],
)
async def test_media_files_contacts_locations_are_not_delivered(tg, fields):
    await paired(tg)
    out = await tg.raw(A, **fields)
    assert [(o.chat_id, o.text) for o in out] == [(A, t.UNSUPPORTED)]


async def test_photos_and_forwards_are_not_delivered(tg):
    await paired(tg)
    out = await tg.photo(A)
    assert [(o.chat_id, o.text) for o in out] == [(A, t.UNSUPPORTED)]
    tg.clock.advance(10)
    out = await tg.forwarded(A, "forwarded text")
    assert [(o.chat_id, o.text) for o in out] == [(A, t.UNSUPPORTED)]
    assert not [o for o in tg.inbox(B) if "forwarded" in o.text]


async def test_rejected_spam_does_not_trigger_reply_spam(tg):
    await paired(tg)
    tg.auto_advance = 0.2
    outs = [await tg.photo(A) for _ in range(5)]
    assert sum(len(o) for o in outs) <= 2  # the notice itself is rate limited


async def test_too_long_and_flood_notices(tg):
    await paired(tg)
    assert (await tg.send(A, "x" * 1001))[0].text == t.TOO_LONG
    tg.auto_advance = 1
    results = [await tg.send(A, "buy my stuff") for _ in range(4)]
    assert [bool([o for o in r if o.chat_id == B]) for r in results] == [True, True, False, False]


async def test_text_from_a_user_in_no_chat_goes_nowhere(tg):
    await tg.onboard(A)
    await tg.onboard(B)
    assert (await tg.send(A, "anybody?"))[0].text == t.NOT_IN_CHAT
    assert tg.inbox(B)[-1].text == t.PROFILE_SAVED


async def test_users_who_have_not_finished_onboarding_cannot_chat_or_be_relayed(tg):
    await tg.send(A, "/start")
    assert (await tg.send(A, "/chat"))[0].text == t.FINISH_SETUP
    assert (await tg.send(A, "hello"))[0].text == t.FINISH_SETUP


async def test_unreachable_partner_ends_the_chat_cleanly(tg, app_pool):
    await paired(tg)
    tg.unreachable.add(B)
    out = await tg.send(A, "hello?")
    assert any(o.text == t.PARTNER_UNREACHABLE for o in out)
    assert await repo_chat.get_partner(app_pool, A) is None


async def test_muted_user_is_told_and_nothing_is_delivered(tg, app_pool):
    from datetime import timedelta

    await paired(tg)
    await repo_safety.add_restriction(app_pool, A, "mute", timedelta(minutes=5), "t")
    out = await tg.send(A, "hello")
    assert [(o.chat_id, o.text) for o in out] == [(A, t.MUTED)]


# ---------------- next / stop / after-chat buttons ----------------


async def test_stop_ends_for_both_and_offers_report_and_block(tg):
    sid = await paired(tg)
    out = await tg.send(A, "/stop")
    assert t.CHAT_ENDED_ME in [o.text for o in out if o.chat_id == A]
    assert t.CHAT_ENDED_PARTNER in [o.text for o in out if o.chat_id == B]
    assert tg.callback_data(A)[-2:] == [f"rep:{sid}", f"blk:{sid}"]
    assert tg.callback_data(B)[-2:] == [f"rep:{sid}", f"blk:{sid}"]
    assert (await tg.send(B, "anyone there?"))[0].text == t.NOT_IN_CHAT


async def test_next_ends_the_chat_and_starts_a_new_search(tg):
    await paired(tg)
    out = await tg.send(A, kb.BTN_NEXT)
    assert t.CHAT_ENDED_PARTNER in [o.text for o in out if o.chat_id == B]
    assert t.SEARCHING in [o.text for o in out if o.chat_id == A]


# ---------------- report / block ----------------


async def test_report_flow_via_command_and_reason_button(tg, app_pool):
    sid = await paired(tg)
    out = await tg.send(A, kb.BTN_REPORT)
    assert out[0].text == t.REPORT_START
    assert f"rr:{sid}:harassment" in [
        b.callback_data for row in out[0].markup.inline_keyboard for b in row
    ]
    out = await tg.press(A, f"rr:{sid}:harassment")
    assert t.REPORT_CREATED in [o.text for o in out]
    assert t.CHAT_ENDED_PARTNER in [o.text for o in out if o.chat_id == B]
    row = await app_pool.fetchrow("SELECT * FROM reports")
    assert (row["reporter_id"], row["reported_id"], row["reason"]) == (A, B, "harassment")
    # reporter and reported are not matched again
    await tg.send(A, "/chat")
    assert (await tg.send(B, "/chat"))[0].text == t.SEARCHING


async def test_report_after_the_chat_via_after_chat_buttons(tg, app_pool):
    sid = await paired(tg)
    await tg.send(B, "/stop")
    out = await tg.press(A, f"rep:{sid}")
    assert any(o.text == t.REPORT_START for o in out)
    out = await tg.press(A, f"rr:{sid}:scam")
    assert t.REPORT_CREATED in [o.text for o in out]
    assert await app_pool.fetchval("SELECT reason FROM reports") == "scam"


async def test_report_outside_a_chat_is_refused(tg):
    await tg.onboard(A)
    assert (await tg.send(A, "/report"))[0].text == t.NOT_IN_CHAT
    assert (await tg.send(A, "/block"))[0].text == t.NOT_IN_CHAT


async def test_stranger_cannot_report_or_block_someone_elses_session(tg, app_pool):
    sid = await paired(tg)
    await tg.onboard(777)
    out = await tg.press(777, f"rr:{sid}:spam")
    assert t.REPORT_NOT_FOUND in [o.text for o in out]
    out = await tg.press(777, f"blk:{sid}")
    assert t.REPORT_NOT_FOUND in [o.text for o in out]
    assert await app_pool.fetchval("SELECT count(*) FROM reports") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM blocks") == 0
    assert await repo_chat.get_partner(app_pool, A) is not None  # chat untouched


@pytest.mark.parametrize(
    "data",
    [
        "rr:abc:spam",
        "rr:1:",
        "rr::spam",
        "rr:1:spam:extra",
        "rr:-1:spam",
        "rr:1:SPAM",
        "rr:1:spam'; DROP TABLE reports;--",
        "rep:99999999999999999999",
        "blk:",
        "blk:x",
        "rep:1 OR 1=1",
        "rr:1:../etc",
        "\x00",
        "",
        "zzz",
    ],
)
async def test_malformed_or_hostile_callback_data_is_ignored(tg, app_pool, data):
    await paired(tg)
    await tg.press(A, data)
    assert await app_pool.fetchval("SELECT count(*) FROM reports") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM blocks") == 0


async def test_unknown_reason_code_for_a_valid_session_is_rejected(tg, app_pool):
    sid = await paired(tg)
    out = await tg.press(A, f"rr:{sid}:xyz")
    assert t.REPORT_NOT_FOUND in [o.text for o in out]
    assert await app_pool.fetchval("SELECT count(*) FROM reports") == 0


async def test_block_command_ends_the_chat_and_prevents_rematch(tg, app_pool):
    await paired(tg)
    out = await tg.send(A, "/block")
    assert t.BLOCK_DONE in [o.text for o in out]
    assert t.CHAT_ENDED_PARTNER in [o.text for o in out if o.chat_id == B]
    assert await app_pool.fetchval("SELECT count(*) FROM blocks WHERE blocker_id = $1", A) == 1
    await tg.send(A, "/chat")
    assert (await tg.send(B, "/chat"))[0].text == t.SEARCHING


async def test_block_button_after_the_chat(tg, app_pool):
    sid = await paired(tg)
    await tg.send(A, "/stop")
    out = await tg.press(B, f"blk:{sid}")
    assert t.BLOCK_DONE in [o.text for o in out]
    assert await app_pool.fetchval("SELECT blocker_id FROM blocks") == B


async def test_report_spam_is_limited(tg, app_pool):
    await tg.onboard(A)
    # build 7 chats worth of sessions directly, then report them all through the buttons
    from tests.factories import ended_session, make_user

    sids = []
    for other in range(100, 107):
        await make_user(app_pool, other)
        sids.append(await ended_session(app_pool, A, other))
    created = 0
    for sid in sids:
        out = await tg.press(A, f"rr:{sid}:spam")
        created += any(o.text == t.REPORT_CREATED for o in out)
    assert created == 5
    assert any(o.text == t.REPORT_RATE_LIMITED for o in tg.out)


# ---------------- privacy of payloads ----------------


async def test_no_telegram_id_appears_in_any_callback_payload(tg):
    sid = await paired(tg)
    await tg.send(A, "/stop")
    await tg.send(A, "/report")  # not in chat any more
    payloads = " ".join(tg.callback_data())
    assert str(A) not in payloads and str(B) not in payloads
    assert f"rep:{sid}" in payloads


async def test_partner_never_sees_username_or_name_in_any_message(tg):
    await tg.onboard(A, name="RealNameA", bio="my bio A")
    await tg.onboard(B, name="RealNameB", bio="my bio B")
    await tg.send(A, "/start", username="alice_tg")
    await tg.send(A, "/chat")
    await tg.send(B, "/chat")
    await tg.send(A, "hi")
    await tg.send(B, "hello")
    await tg.send(A, "/stop")
    to_a = " ".join(o.text for o in tg.inbox(A) if o.chat_id == A)
    to_b = " ".join(o.text for o in tg.inbox(B))
    for leaked in ("RealNameA", "my bio A", "alice_tg"):
        assert leaked not in to_b
    for leaked in ("RealNameB", "my bio B"):
        assert leaked not in to_a
