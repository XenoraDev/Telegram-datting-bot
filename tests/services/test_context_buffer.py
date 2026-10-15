"""Privacy model: report context is RAM-only, last 10 text messages, 10 minute lifetime."""

from bot.services.chat_relay import BUFFER_MAX_MESSAGES, BUFFER_TTL_SECONDS, ContextBuffer


def test_keeps_only_the_last_ten_messages(clock):
    buf = ContextBuffer(clock)
    for i in range(25):
        buf.add(1, 100, f"m{i}")
        clock.advance(1)
    snap = buf.snapshot_for_report(1, 100)
    assert [m["text"] for m in snap] == [f"m{i}" for i in range(15, 25)]
    assert len(snap) == BUFFER_MAX_MESSAGES == 10


def test_messages_expire_after_ten_minutes(clock):
    buf = ContextBuffer(clock)
    buf.add(1, 100, "old")
    clock.advance(BUFFER_TTL_SECONDS - 1)
    buf.add(1, 200, "fresh")
    clock.advance(2)  # "old" is now 601s old, "fresh" 2s
    assert [m["text"] for m in buf.snapshot_for_report(1, 100)] == ["fresh"]
    clock.advance(BUFFER_TTL_SECONDS)
    assert buf.snapshot_for_report(1, 100) is None
    assert len(buf) == 0  # empty sessions are forgotten


def test_snapshot_uses_roles_never_user_ids(clock):
    buf = ContextBuffer(clock)
    buf.add(1, 111, "hi")
    buf.add(1, 222, "hello")
    snap = buf.snapshot_for_report(1, 111)
    assert [(m["from"], m["text"]) for m in snap] == [("reporter", "hi"), ("reported", "hello")]
    assert "111" not in str(snap) and "222" not in str(snap)
    assert all(set(m) == {"from", "text", "at"} for m in snap)


def test_sessions_are_isolated_and_droppable(clock):
    buf = ContextBuffer(clock)
    buf.add(1, 1, "a")
    buf.add(2, 2, "b")
    buf.drop(1)
    assert buf.snapshot_for_report(1, 1) is None
    assert buf.snapshot_for_report(2, 2) is not None


def test_prune_releases_expired_sessions(clock):
    buf = ContextBuffer(clock)
    for sid in range(50):
        buf.add(sid, 1, "x")
    clock.advance(BUFFER_TTL_SECONDS + 1)
    assert buf.prune() == 0


def test_long_text_is_truncated(clock):
    buf = ContextBuffer(clock)
    buf.add(1, 1, "x" * 5000)
    assert len(buf.snapshot_for_report(1, 1)[0]["text"]) <= 1000
