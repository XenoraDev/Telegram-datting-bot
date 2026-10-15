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
        ("add me @bob_99", "contact"),
        ("call 9876543210", "contact"),
    ],
)
async def test_links_and_contact_details_are_blocked(svc, pair, t, status):
    assert (await svc.relay.relay(1, text(t))).status == status


@pytest.mark.parametrize(
    "etype", ["url", "text_link", "mention", "email", "phone_number", "text_mention"]
)
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
