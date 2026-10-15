"""Privacy invariants: chat content is never persisted; contacts are never revealed."""

from bot.services.chat_relay import Incoming
from tests.factories import make_user, start_chat

MARKER = "zx9-very-private-marker-q7"


async def dump_all_tables(pool) -> str:
    tables = [
        r["tablename"]
        for r in await pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    ]
    parts = []
    for t in tables:
        rows = await pool.fetch(f'SELECT row_to_json(x)::text AS j FROM "{t}" x')  # noqa: S608
        parts.extend(r["j"] for r in rows)
    return "\n".join(parts)


async def test_chat_text_never_reaches_the_database(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    sid = await start_chat(svc, 1, 2)
    for i in range(5):
        assert (await svc.relay.relay(1, Incoming("text", text=f"{MARKER} {i}"))).status == "ok"
        assert (
            await svc.relay.relay(2, Incoming("text", text=f"reply {MARKER} {i}"))
        ).status == "ok"
    assert (await svc.relay.relay(1, Incoming("sticker", sticker_id="STICKER_ID_X"))).status == "ok"
    await svc.matchmaking.stop(1)
    dump = await dump_all_tables(app_pool)
    assert MARKER not in dump
    assert "STICKER_ID_X" not in dump
    assert f'"id":{sid}' in dump  # sanity: the dump does contain the session metadata


async def test_only_a_report_copies_text_into_the_database_and_only_into_reports_context(
    app_pool, svc
):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    sid = await start_chat(svc, 1, 2)
    await svc.relay.relay(2, Incoming("text", text=f"abusive {MARKER}"))
    await svc.moderation.report(1, sid, "harassment")
    hits = [
        r["tablename"]
        for r in await app_pool.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        if MARKER
        in str(
            await app_pool.fetchval(f"SELECT string_agg(x::text, ' ') FROM \"{r['tablename']}\" x")
        )  # noqa: S608
    ]
    assert hits == ["reports"]


async def test_schema_has_no_message_columns_and_only_one_json_column(app_pool):
    cols = await app_pool.fetch(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public'"
    )
    names = {(c["table_name"], c["column_name"]) for c in cols}
    forbidden = {"message", "messages", "msg", "body", "content", "text", "payload"}
    assert not [n for n in names if n[1] in forbidden]
    assert not [
        n for n in names if n[0] in ("messages", "chat_messages", "chat_history", "history")
    ]
    json_cols = {
        (c["table_name"], c["column_name"]) for c in cols if c["data_type"] in ("json", "jsonb")
    }
    assert json_cols == {("reports", "context")}


async def test_partner_sees_only_age_gender_city_never_identity_columns(app_pool, svc):
    await make_user(app_pool, 1, gender="f", age=31, city="Oslo")
    await make_user(app_pool, 2)
    await svc.matchmaking.search(1)
    res = await svc.matchmaking.search(2)
    peer = res.partner
    # Peer is the only object handed to the Telegram layer: it has no username / name / bio
    assert set(peer.__slots__) == {"user_id", "age", "gender", "city"}


async def test_contact_details_cannot_be_exchanged_in_chat(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await start_chat(svc, 1, 2)
    for leak in ("@my_username", "t.me/my_username", "+49 170 1234567", "me@example.com"):
        assert (await svc.relay.relay(1, Incoming("text", text=leak))).status in ("link", "contact")
    assert (
        await svc.relay.relay(1, Incoming("other"))
    ).status == "unsupported"  # contact card etc.


async def test_report_context_lifetime_is_30_days(app_pool):
    life = await app_pool.fetchval(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'reports' AND column_name = 'context_expires_at'"
    )
    assert "30 days" in life
