"""What may pass through the anonymous chat: text + stickers only, no links/contacts."""

import pytest

from bot.db import repo_safety
from bot.services.chat_relay import Incoming
from tests.factories import make_user, start_chat


@pytest.fixture
async def pair(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    sid = await start_chat(svc, 1, 2)
    return 1, 2, sid


def text(t, **kw):
    return Incoming("text", text=t, **kw)


async def test_text_is_relayed_to_the_partner_only(svc, pair):
    a, b, sid = pair
    res = await svc.relay.relay(a, text("hello"))
    assert (res.status, res.partner_id, res.session_id, res.text) == ("ok", b, sid, "hello")
    assert (await svc.relay.relay(b, text("hi back"))).partner_id == a


async def test_stickers_are_relayed(svc, pair):
    res = await svc.relay.relay(1, Incoming("sticker", sticker_id="STK"))
    assert (res.status, res.sticker_id) == ("ok", "STK")


async def test_user_outside_a_chat_cannot_relay(app_pool, svc):
    await make_user(app_pool, 5)
    assert (await svc.relay.relay(5, text("anyone?"))).status == "not_in_chat"


@pytest.mark.parametrize(
    "msg",
    [
        Incoming("other"),  # photo / video / voice / document / location / poll ...
        Incoming("text", text="hi", forwarded=True),
        Incoming("text", text="hi", via_bot=True),
        Incoming("sticker", sticker_id="S", forwarded=True),
        Incoming("sticker", sticker_id=None),
    ],
)
async def test_unsupported_content_is_never_relayed(svc, pair, msg):
    assert (await svc.relay.relay(1, msg)).status == "unsupported"


@pytest.mark.parametrize(
    "t,status",
    [
        ("see http://evil.com", "link"),
        ("evil.com", "link"),
        ("t.me/x", "link"),
        ("call 9876543210", "contact"),
        ("+91 98765 43210", "contact"),
        ("john (at) gmail (dot) com", "link"),
    ],
)
async def test_links_and_contact_details_are_blocked(svc, pair, t, status):
    assert (await svc.relay.relay(1, text(t))).status == status


@pytest.mark.parametrize("etype", ["url", "text_link", "email", "phone_number", "text_mention"])
async def test_link_entities_are_blocked_even_if_the_text_looks_harmless(svc, pair, etype):
    res = await svc.relay.relay(1, text("click here", entity_types=(etype,)))
    assert res.status == "link"


async def test_blocked_content_is_not_recorded_in_the_report_buffer(svc, pair):
    a, _, sid = pair
    await svc.relay.relay(a, text("http://evil.com"))
    assert svc.buffer.snapshot_for_report(sid, a) is None


async def test_empty_and_too_long_messages(svc, pair):
    assert (await svc.relay.relay(1, text("   "))).status == "empty"
    assert (await svc.relay.relay(1, text("x" * 1001))).status == "too_long"
    assert (await svc.relay.relay(1, text("x" * 1000))).status == "ok"


async def test_repeating_the_same_message_is_flood(svc, pair, clock):
    statuses = []
    for _ in range(4):
        statuses.append((await svc.relay.relay(1, text("buy now"))).status)
        clock.advance(1)
    assert statuses == ["ok", "ok", "flood", "flood"]
    clock.advance(60)  # window passed
    assert (await svc.relay.relay(1, text("buy now"))).status == "ok"


async def test_different_messages_are_not_flood(svc, pair, clock):
    for i in range(6):
        assert (await svc.relay.relay(1, text(f"message {i}"))).status == "ok"
        clock.advance(1)


async def test_chat_message_rate_limit(svc, pair):
    statuses = [(await svc.relay.relay(1, text(f"m{i}"))).status for i in range(12)]
    assert statuses[:8] == ["ok"] * 8
    assert "rate_limited" in statuses[8:]


async def test_muted_sender_cannot_relay_but_can_still_receive(app_pool, svc, pair):
    from datetime import timedelta

    a, b, _ = pair
    await repo_safety.add_restriction(app_pool, a, "mute", timedelta(minutes=5), "t")
    assert (await svc.relay.relay(a, text("hello"))).status == "muted"
    assert (await svc.relay.relay(b, text("hello"))).status == "ok"


async def test_banned_or_deleted_partner_stops_the_relay(app_pool, svc, pair):
    a, b, _ = pair
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", b)
    assert (await svc.relay.relay(a, text("still there?"))).status == "not_in_chat"
    assert (await svc.relay.relay(b, text("let me in"))).status == "not_in_chat"


async def test_ended_chat_cannot_relay(svc, pair):
    a, b, _ = pair
    await svc.matchmaking.stop(a)
    assert (await svc.relay.relay(a, text("hi"))).status == "not_in_chat"
    assert (await svc.relay.relay(b, text("hi"))).status == "not_in_chat"


async def test_flood_tracking_state_is_pruned_when_users_go_quiet(svc, pair, clock):
    await svc.relay.relay(1, text("hello"))
    await svc.relay.relay(2, text("hi"))
    assert svc.relay.prune() == 2
    clock.advance(31)
    assert svc.relay.prune() == 0
    assert svc.relay._flood.recent == {}


async def test_telegram_usernames_are_allowed_in_chat(svc, pair):
    for t in ("add me @rahul123", "@bob_99", "find me on telegram: @Some_User"):
        assert (await svc.relay.relay(1, text(t))).status == "ok", t
    res = await svc.relay.relay(2, text("hi @rahul123", entity_types=("mention",)))
    assert res.status == "ok"
    assert (await svc.relay.relay(1, text("https://t.me/rahul123"))).status == "link"


async def test_phone_split_over_consecutive_messages_is_blocked(svc, pair):
    assert (await svc.relay.relay(1, text("98765"))).status == "ok"  # a fragment alone is harmless
    assert (await svc.relay.relay(1, text("43210"))).status == "contact"
    assert len(svc.relay.contacts) == 0  # state is cleared after a block


async def test_split_phone_in_three_parts_and_with_filler_message_between(svc, pair):
    for part in ("98765", "432"):
        assert (await svc.relay.relay(1, text(part))).status == "ok"
    assert (await svc.relay.relay(1, text("10"))).status == "contact"
    assert (await svc.relay.relay(1, text("98765"))).status == "ok"
    assert (await svc.relay.relay(1, text("lol"))).status == "ok"
    assert (await svc.relay.relay(1, text("43210"))).status == "contact"


async def test_split_state_is_per_sender_not_shared_with_the_partner(svc, pair):
    assert (await svc.relay.relay(1, text("98765"))).status == "ok"
    assert (await svc.relay.relay(2, text("43210"))).status == "ok"


async def test_split_number_expires_after_the_ttl_and_a_few_messages(app_pool, svc, pair, clock):
    from bot.services.chat_relay import CARRY_MAX_MISSES, CARRY_TTL_SECONDS

    assert (await svc.relay.relay(1, text("98765"))).status == "ok"
    clock.advance(CARRY_TTL_SECONDS + 1)
    assert (await svc.relay.relay(1, text("43210"))).status == "ok"

    assert (await svc.relay.relay(2, text("98765"))).status == "ok"
    for i in range(CARRY_MAX_MISSES + 1):
        clock.advance(1)
        assert (await svc.relay.relay(2, text(f"word {chr(97 + i)}"))).status == "ok"
    assert (await svc.relay.relay(2, text("43210"))).status == "ok"


async def test_cue_in_an_earlier_message_applies_to_the_following_digits(svc, pair):
    assert (await svc.relay.relay(1, text("my whatsapp number"))).status == "ok"
    assert (await svc.relay.relay(1, text("12345678"))).status == "contact"


async def test_plain_numbers_are_still_usable(svc, pair):
    for t in (
        "12345678",
        "I scored 85 92 in maths",
        "on 12/05/2024 at 10:30",
        "it costs 1,00,00,000",
    ):
        assert (await svc.relay.relay(1, text(t))).status == "ok", t
        assert (await svc.relay.relay(2, text(t + " ok"))).status == "ok", t


async def test_contact_tracker_is_bounded_and_keeps_digits_only(svc, pair):
    from bot.services.chat_relay import CARRY_MAX_ENTRIES, ContactTracker
    from bot.services.content_rules import PhoneCarry

    tr = ContactTracker()
    for sid in range(CARRY_MAX_ENTRIES + 500):
        tr.update(sid, 1, PhoneCarry(), PhoneCarry("98765", False))
    assert len(tr) == CARRY_MAX_ENTRIES
    assert tr.get(0, 1) == PhoneCarry()  # oldest evicted
    assert tr.get(CARRY_MAX_ENTRIES + 499, 1).digits == "98765"

    await svc.relay.relay(1, text("my number is secret text 123456789012345678901234567890"))
    stored = [c for (_, _, c) in svc.relay.contacts._state.values()]
    assert all(len(c.digits) <= 15 and c.digits.isdigit() for c in stored)


async def test_nothing_from_the_tracker_reaches_the_report_buffer(app_pool, svc, pair):
    await svc.relay.relay(1, text("98765"))
    snap = svc.buffer.snapshot_for_report(pair[2], 1)
    assert [m["text"] for m in snap] == ["98765"]  # same as before: only delivered text
    assert svc.relay.contacts.prune() == 1
