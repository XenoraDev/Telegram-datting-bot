"""The Telegram payment flow end to end: Premium -> plan -> pay -> screenshot -> UTR -> pending.

Nothing here can activate premium; only the admin service can (see test_payments.py)."""

import pytest

from bot import keyboards as kb
from bot.texts.i18n import tr
from tests import payment_images as img
from tests.factories import enable_payments, make_plan
from tests.handlers.harness import ADMIN_ID

U1, U2 = 7001, 7002


@pytest.fixture
async def shop(app_pool):
    await enable_payments(app_pool)
    return await make_plan(app_pool)


def last_text(out):
    """The last message sent to the user (admin alerts are a different chat)."""
    return [o.text for o in out if o.kind == "SendMessage" and o.chat_id != ADMIN_ID][-1]


def cbs(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


async def open_order(tg, uid, plan):
    out = await tg.press(uid, f"pm:s:{plan}")
    pid = int([c for c in cbs(out) if c.startswith("pm:u:")][0].split(":")[2])
    return pid, out


async def test_premium_is_in_the_menu_and_lists_the_plans(tg, shop):
    await tg.onboard(U1)
    out = await tg.send(U1, kb.BTN_PREMIUM)
    assert cbs(out) == [f"pm:s:{shop}"]
    text = last_text(out)
    assert "Premium" in text and "Choose a plan" in text
    button = out[-1].markup.inline_keyboard[0][0].text
    assert button == "Premium 30 Days — ₹99 / 30 days"
    assert kb.BTN_PREMIUM in [b.text for row in kb.main_menu("en").keyboard for b in row]
    assert cbs(await tg.send(U1, "/premium")) == [f"pm:s:{shop}"]


async def test_choosing_a_plan_shows_amount_reference_upi_and_a_qr(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, out = await open_order(tg, U1, shop)
    photo = [o for o in out if o.kind == "SendPhoto"]
    assert len(photo) == 1 and "₹99" in photo[0].method.caption
    ref = await app_pool.fetchval("SELECT reference FROM payments WHERE id=$1", pid)
    text = last_text(out)
    for needle in (
        "₹99",
        "30 days",
        ref,
        "shop@okbank",
        "Test Payee",
        "Pay then upload the screenshot.",
    ):
        assert needle in text
    assert "starts after an admin" in text
    assert set(cbs(out)) == {f"pm:u:{pid}", f"pm:c:{pid}"}
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0


async def test_full_flow_ends_pending_and_never_premium(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    out = await tg.press(U1, f"pm:u:{pid}")
    assert "Send a screenshot" in last_text(out)
    out = await tg.photo_bytes(U1, img.JPEG)
    assert "Screenshot received" in last_text(out) and cbs(out) == [f"pm:k:{pid}"]
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"
    out = await tg.send(U1, "ab12cd34ef56")
    ref = await app_pool.fetchval("SELECT reference FROM payments")
    assert ref in last_text(out) and "submitted for verification" in last_text(out)
    row = await app_pool.fetchrow("SELECT status, utr FROM payments")
    assert (row["status"], row["utr"]) == ("pending", "AB12CD34EF56")
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    # the admin is told that a payment waits (and nothing else)
    admin_msgs = tg.texts(ADMIN_ID)
    assert any(ref in m and "₹99" in m for m in admin_msgs)
    # the Premium screen now shows it as waiting and offers no way to start another payment
    out = await tg.send(U1, kb.BTN_PREMIUM)
    assert "waiting for verification" in last_text(out) and cbs(out) == []


async def test_skipping_the_utr_still_submits(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    await tg.press(U1, f"pm:u:{pid}")
    await tg.photo_bytes(U1, img.PNG)
    out = await tg.press(U1, f"pm:k:{pid}")
    assert "submitted for verification" in last_text(out)
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"
    assert await app_pool.fetchval("SELECT utr FROM payments") is None


async def test_image_sent_as_a_file_is_accepted_too(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    await tg.press(U1, f"pm:u:{pid}")
    out = await tg.document_bytes(U1, img.PNG, mime="image/png", name="../../etc/passwd.png")
    assert "Screenshot received" in last_text(out)
    assert await app_pool.fetchval("SELECT content_type FROM payment_evidence") == "image/png"


@pytest.mark.parametrize(
    "send,expected",
    [
        (lambda tg: tg.photo_bytes(U1, b"<?php system('id'); ?>" * 5), "not a valid image"),
        (lambda tg: tg.photo_bytes(U1, img.JPEG[:-30]), "damaged or incomplete"),
        (lambda tg: tg.photo_bytes(U1, img.JPEG, declared=10 * 1024 * 1024), "too large"),
        (
            lambda tg: tg.document_bytes(U1, img.PNG, mime="application/pdf", name="x.pdf"),
            "not a valid image",
        ),
        (
            lambda tg: tg.document_bytes(
                U1, b"MZ" + b"\0" * 100, mime="image/png", name="evil.png"
            ),
            "not a valid image",
        ),
        (lambda tg: tg.send(U1, "here is my screenshot"), "photo or an image file"),
        (lambda tg: tg.sticker(U1), "photo or an image file"),
    ],
)
async def test_bad_uploads_are_refused_and_nothing_is_stored(tg, shop, app_pool, send, expected):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    await tg.press(U1, f"pm:u:{pid}")
    out = await send(tg)
    assert expected in last_text(out)
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"
    out = await tg.photo_bytes(U1, img.JPEG, file_id="GOOD")  # the user can simply retry
    assert "Screenshot received" in last_text(out)


async def test_invalid_and_duplicate_utr_keep_the_user_in_the_step(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    pid, _ = await open_order(tg, U1, shop)
    await tg.press(U1, f"pm:u:{pid}")
    await tg.photo_bytes(U1, img.JPEG)
    await tg.send(U1, "UTR000000042")
    pid2, _ = await open_order(tg, U2, shop)
    await tg.press(U2, f"pm:u:{pid2}")
    await tg.photo_bytes(U2, img.PNG, file_id="S2")
    assert "does not look right" in last_text(await tg.send(U2, "12!"))
    assert "already used" in last_text(await tg.send(U2, "utr000000042"))
    assert await app_pool.fetchval("SELECT status FROM payments WHERE id=$1", pid2) == "created"
    assert "submitted for verification" in last_text(await tg.send(U2, "UTR000000043"))


async def test_cancelling_an_order_and_stale_buttons(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    out = await tg.press(U1, f"pm:c:{pid}")
    assert "cancelled" in last_text(out).lower()
    assert await app_pool.fetchval("SELECT status FROM payments") == "cancelled"
    await tg.press(U1, f"pm:u:{pid}")
    assert any("no longer valid" in a.text for a in tg.callback_answers() if a.text)


async def test_nobody_can_use_someone_elses_payment_buttons(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    pid, _ = await open_order(tg, U1, shop)
    for verb in ("u", "c", "k"):
        await tg.press(U2, f"pm:{verb}:{pid}")
    assert await app_pool.fetchval("SELECT status FROM payments WHERE id=$1", pid) == "created"
    assert (await tg.photo_bytes(U2, img.JPEG)) is not None
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0


@pytest.mark.parametrize(
    "data", ["pm:s:0", "pm:s:x", "pm:z:1", "pm:u:99999999999999", "pm:s:1:2", "pm:"]
)
async def test_forged_callback_data_is_ignored(tg, shop, app_pool, data):
    await tg.onboard(U1)
    await tg.press(U1, data)
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 0


async def test_unconfigured_or_empty_shop_says_so(tg, app_pool):
    await tg.onboard(U1)
    assert "Payments are not available" in last_text(await tg.send(U1, kb.BTN_PREMIUM))
    text = last_text(await tg.send(U1, "/premium"))
    assert "Payments are not available" in text
    await enable_payments(app_pool)
    assert "Premium plans are not available" in last_text(await tg.send(U1, "/premium"))


async def test_a_user_who_has_not_finished_onboarding_cannot_open_premium(tg, shop):
    await tg.send(U1, "/start")
    out = await tg.send(U1, "/premium")
    assert cbs(out) == [] and all("Choose a plan" not in o.text for o in out)


async def test_premium_is_blocked_during_a_random_chat(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    await tg.send(U1, kb.BTN_CHAT)
    await tg.send(U2, kb.BTN_CHAT)
    out = await tg.send(U1, "/premium")
    assert "Finish your chat first" in last_text(out)
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 0
    # and the chat itself is untouched: a normal message is still relayed
    out = await tg.send(U1, "hello partner")
    assert any("hello partner" in (o.text or "") for o in out if o.chat_id == U2)


async def test_menu_buttons_and_commands_leave_the_payment_conversation(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    await tg.press(U1, f"pm:u:{pid}")
    out = await tg.send(U1, kb.BTN_LANGUAGE)  # a menu button, not a UTR
    assert cbs(out) == ["lg:en", "lg:hi"]
    # the conversation is over: free text is no longer taken as a UTR
    out = await tg.send(U1, "UTR123456789")
    assert "submitted" not in last_text(out)
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"


async def test_chat_messages_are_not_swallowed_by_a_forgotten_payment_state(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    pid, _ = await open_order(tg, U1, shop)
    await tg.press(U1, f"pm:u:{pid}")
    await tg.photo_bytes(U1, img.JPEG)  # now waiting for a UTR
    await tg.send(U1, kb.BTN_CHAT)  # menu button leaves the flow and starts a search
    await tg.send(U2, kb.BTN_CHAT)
    await tg.send(U1, "hello there friend")  # this is chat text now, not a UTR
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"
    assert any("hello there friend" in (o.text or "") for o in tg.inbox(U2))


async def test_the_payment_flow_is_rate_limited(tg, shop):
    await tg.onboard(U1)
    seen = []
    for _ in range(9):
        out = await tg.press(U1, f"pm:s:{shop}")
        seen.append(last_text(out) if out else "")
    assert any("Too many attempts" in s for s in seen)


# ---------------------------------------------------------------- Hindi


async def test_the_whole_flow_is_in_hindi_for_a_hindi_user(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.press(U1, "lg:hi")
    out = await tg.send(U1, tr("btn.premium", "hi"))
    assert "प्रीमियम" in last_text(out) and "एक प्लान चुनें" in last_text(out)
    assert out[-1].markup.inline_keyboard[0][0].text == "Premium 30 Days — ₹99 / 30 दिन"
    pid, out = await open_order(tg, U1, shop)
    text = last_text(out)
    assert "राशि" in text and "UPI ID" in text and "मंज़ूर" in text
    labels = [b.text for row in out[-1].markup.inline_keyboard for b in row]
    assert labels == [tr("btn.upload_proof", "hi"), tr("btn.cancel_payment", "hi")]
    out = await tg.press(U1, f"pm:u:{pid}")
    assert "स्क्रीनशॉट" in last_text(out)
    assert "सही इमेज नहीं है" in last_text(await tg.photo_bytes(U1, b"junk" * 30))
    out = await tg.photo_bytes(U1, img.JPEG, file_id="HI1")
    assert "स्क्रीनशॉट मिल गया" in last_text(out)
    out = await tg.press(U1, f"pm:k:{pid}")
    assert "जाँच के लिए भेज दिया गया" in last_text(out)
    assert tr("btn.premium", "hi") in [b.text for row in kb.main_menu("hi").keyboard for b in row]
