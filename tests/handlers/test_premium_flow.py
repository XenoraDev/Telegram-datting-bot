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
    pid = int([c for c in cbs(out) if c.startswith("pm:c:")][0].split(":")[2])
    return pid, out


async def test_premium_is_in_the_menu_and_lists_the_plans(tg, shop):
    await tg.onboard(U1)
    out = await tg.send(U1, kb.BTN_PREMIUM)
    assert cbs(out) == [f"pm:s:{shop}", "pm:h"]
    text = last_text(out)
    assert "Premium" in text and "Choose a plan" in text
    button = out[-1].markup.inline_keyboard[0][0].text
    assert button == "Premium 30 Days — ₹99 / 30 days"
    assert kb.BTN_PREMIUM in [b.text for row in kb.main_menu("en").keyboard for b in row]
    assert cbs(await tg.send(U1, "/premium")) == [f"pm:s:{shop}", "pm:h"]


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
    assert cbs(out) == [f"pm:c:{pid}"]
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0


async def test_choosing_a_plan_asks_for_the_photo_with_no_button_to_press_first(tg, shop):
    await tg.onboard(U1)
    pid, out = await open_order(tg, U1, shop)
    text = last_text(out)
    assert "send the payment screenshot here as a photo" in text and "Nothing else to tap" in text
    assert "UTR" in text  # the optional UTR is explained right away
    assert cbs(out) == [f"pm:c:{pid}"]  # only "cancel": there is no "I have paid" button
    assert "I have paid" not in " ".join(o.text for o in out)


async def test_a_photo_sent_straight_away_submits_the_payment(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    ref = await app_pool.fetchval("SELECT reference FROM payments")
    out = await tg.photo_bytes(U1, img.JPEG)
    assert ref in last_text(out) and "submitted for verification" in last_text(out)
    assert "Optional: send your UTR" in last_text(out)
    row = await app_pool.fetchrow("SELECT status, utr, submitted_at FROM payments")
    assert row["status"] == "pending" and row["utr"] is None and row["submitted_at"]
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 1
    assert await app_pool.fetchval("SELECT count(*) FROM subscriptions") == 0
    assert any(ref in m and "₹99" in m for m in tg.texts(ADMIN_ID))  # the admin is told
    out = await tg.send(U1, kb.BTN_PREMIUM)  # now waiting; no way to start another order
    assert "waiting for verification" in last_text(out) and cbs(out) == ["pm:h"]


async def test_the_utr_can_follow_as_a_message(tg, shop, app_pool):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    await tg.photo_bytes(U1, img.JPEG)
    out = await tg.send(U1, "ab12cd34ef56")
    assert "UTR saved" in last_text(out)
    row = await app_pool.fetchrow("SELECT status, utr FROM payments")
    assert (row["status"], row["utr"]) == ("pending", "AB12CD34EF56")
    out = await tg.send(U1, "ZZ99ZZ99ZZ99")  # the step is over: this is not a UTR any more
    assert "saved" not in last_text(out)
    assert await app_pool.fetchval("SELECT utr FROM payments") == "AB12CD34EF56"


async def test_the_utr_can_be_the_photo_caption(tg, shop, app_pool):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    tg.captions[U1] = "utr 4123 4567 8901"
    out = await tg.photo_bytes(U1, img.JPEG)
    assert "submitted for verification" in last_text(out) and "Optional" not in last_text(out)
    assert await app_pool.fetchval("SELECT utr FROM payments") == "412345678901"
    assert "Optional" not in last_text(out)


async def test_a_bad_caption_does_not_block_the_submission(tg, shop, app_pool):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    tg.captions[U1] = "paid!!"
    out = await tg.photo_bytes(U1, img.JPEG)
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"
    assert any("does not look right" in o.text for o in out)


async def test_a_photo_without_an_order_creates_nothing_and_says_what_to_do(tg, shop, app_pool):
    await tg.onboard(U1)
    out = await tg.photo_bytes(U1, img.JPEG)
    assert "choose a plan" in last_text(out)
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 0
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0


async def test_a_cancelled_or_expired_order_no_longer_takes_photos(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    await tg.press(U1, f"pm:c:{pid}")
    assert "choose a plan" in last_text(await tg.photo_bytes(U1, img.JPEG))
    pid, _ = await open_order(tg, U1, shop)
    await app_pool.execute(
        "UPDATE payments SET created_at = now() - interval '25 hours' WHERE status = 'created'"
    )
    await tg.services.payments.maintain()  # stale orders are cancelled
    assert await app_pool.fetchval("SELECT status FROM payments WHERE id=$1", pid) == "cancelled"
    assert "choose a plan" in last_text(await tg.photo_bytes(U1, img.JPEG, file_id="X2"))
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0


async def test_a_second_screenshot_is_refused_and_changes_nothing(tg, shop, app_pool):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    await tg.photo_bytes(U1, img.JPEG)
    sha = await app_pool.fetchval("SELECT sha256 FROM payment_evidence")
    tg.clock.advance(60)
    out = await tg.photo_bytes(U1, img.PNG, file_id="SECOND")
    assert "already submitted" in last_text(out)
    assert await app_pool.fetchval("SELECT sha256 FROM payment_evidence") == sha
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 1


async def test_simultaneous_photos_make_one_submission(tg, shop, app_pool):
    import asyncio

    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    tg.auto_advance = 0.0
    results = await asyncio.gather(
        *(tg.services.payments.submit_screenshot(U1, img.JPEG) for _ in range(5))
    )
    assert sorted(r.kind for r in results).count("ok") == 1
    assert {r.kind for r in results} <= {"ok", "already_submitted", "rate_limited"}
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 1


async def test_image_sent_as_a_file_is_accepted_too(tg, shop, app_pool):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    out = await tg.document_bytes(U1, img.PNG, mime="image/png", name="../../etc/passwd.png")
    assert "submitted for verification" in last_text(out)
    assert await app_pool.fetchval("SELECT content_type FROM payment_evidence") == "image/png"


@pytest.mark.parametrize(
    "send,expected",
    [
        (lambda tg: tg.photo_bytes(U1, b"<?php system('id'); ?>" * 5), "not a valid image"),
        (lambda tg: tg.photo_bytes(U1, img.JPEG[:-30]), "damaged or incomplete"),
        (lambda tg: tg.photo_bytes(U1, img.JPEG, declared=10 * 1024 * 1024), "too large"),
        (
            lambda tg: tg.document_bytes(
                U1, b"MZ" + b"\0" * 100, mime="image/png", name="evil.png"
            ),
            "not a valid image",
        ),
        (lambda tg: tg.photo_bytes(U1, img.PNG + b"<script>"), "damaged or incomplete"),
    ],
)
async def test_bad_uploads_are_refused_and_nothing_is_stored(tg, shop, app_pool, send, expected):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    out = await send(tg)
    assert expected in last_text(out)
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"
    out = await tg.photo_bytes(U1, img.JPEG, file_id="GOOD")  # the user can simply retry
    assert "submitted for verification" in last_text(out)


async def test_a_pdf_or_text_is_not_payment_evidence(tg, shop, app_pool):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    await tg.document_bytes(U1, img.PNG, mime="application/pdf", name="x.pdf")
    await tg.send(U1, "here is my screenshot")
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"


async def test_invalid_and_duplicate_utr_messages_keep_the_user_in_the_step(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    await open_order(tg, U1, shop)
    await tg.photo_bytes(U1, img.JPEG)
    await tg.send(U1, "UTR000000042")
    await open_order(tg, U2, shop)
    await tg.photo_bytes(U2, img.PNG, file_id="S2")
    assert "does not look right" in last_text(await tg.send(U2, "12!"))
    assert "already used" in last_text(await tg.send(U2, "utr000000042"))
    assert await app_pool.fetchval("SELECT utr FROM payments WHERE user_id=$1", U2) is None
    assert "UTR saved" in last_text(await tg.send(U2, "UTR000000043"))


async def test_a_caption_utr_used_elsewhere_refuses_the_whole_submission(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    await open_order(tg, U1, shop)
    tg.captions[U1] = "UTR000000042"
    await tg.photo_bytes(U1, img.JPEG)
    await open_order(tg, U2, shop)
    tg.captions[U2] = "UTR000000042"
    out = await tg.photo_bytes(U2, img.PNG, file_id="S2")
    assert "already used" in last_text(out)
    assert await app_pool.fetchval("SELECT status FROM payments WHERE user_id=$1", U2) == "created"


async def test_cancelling_an_order_and_stale_buttons(tg, shop, app_pool):
    await tg.onboard(U1)
    pid, _ = await open_order(tg, U1, shop)
    out = await tg.press(U1, f"pm:c:{pid}")
    assert "cancelled" in last_text(out).lower()
    assert await app_pool.fetchval("SELECT status FROM payments") == "cancelled"
    await tg.press(U1, f"pm:u:{pid}")  # an old "upload" button
    assert any("no longer valid" in a.text for a in tg.callback_answers() if a.text)


async def test_nobody_can_use_someone_elses_payment(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    pid, _ = await open_order(tg, U1, shop)
    for verb in ("u", "c", "k"):
        await tg.press(U2, f"pm:{verb}:{pid}")
    assert await app_pool.fetchval("SELECT status FROM payments WHERE id=$1", pid) == "created"
    out = await tg.photo_bytes(U2, img.JPEG)  # U2 has no order of their own
    assert "choose a plan" in last_text(out)
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0
    await tg.photo_bytes(U1, img.JPEG, file_id="MINE")  # the evidence lands on U1's order only
    assert await app_pool.fetchval("SELECT user_id FROM payments WHERE status='pending'") == U1


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


async def test_menu_buttons_and_commands_leave_the_utr_step(tg, shop, app_pool):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    await tg.photo_bytes(U1, img.JPEG)  # now optionally waiting for a UTR
    out = await tg.send(U1, kb.BTN_LANGUAGE)  # a menu button, not a UTR
    assert cbs(out) == ["lg:en", "lg:hi"]
    await tg.send(U1, "hello")  # whatever happens next, a payment is never changed by it
    assert await app_pool.fetchval("SELECT status FROM payments") == "pending"


async def test_chat_messages_are_not_swallowed_by_a_forgotten_utr_step(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    await open_order(tg, U1, shop)
    await tg.photo_bytes(U1, img.JPEG)  # now waiting for a UTR
    await tg.send(U1, kb.BTN_CHAT)  # menu button leaves the step and starts a search
    await tg.send(U2, kb.BTN_CHAT)
    await tg.send(U1, "hello there friend")  # this is chat text now, not a UTR
    assert await app_pool.fetchval("SELECT utr FROM payments") is None
    assert any("hello there friend" in (o.text or "") for o in tg.inbox(U2))


async def test_a_photo_during_a_chat_is_never_payment_evidence(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    await open_order(tg, U1, shop)
    await tg.send(U1, kb.BTN_CHAT)
    await tg.send(U2, kb.BTN_CHAT)
    await tg.photo_bytes(U1, img.JPEG)
    assert await app_pool.fetchval("SELECT count(*) FROM payment_evidence") == 0
    assert await app_pool.fetchval("SELECT status FROM payments") == "created"


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
    assert [b.text for row in out[-1].markup.inline_keyboard for b in row] == [
        tr("btn.cancel_payment", "hi")
    ]
    assert "सही इमेज नहीं है" in last_text(await tg.photo_bytes(U1, b"junk" * 30))
    out = await tg.photo_bytes(U1, img.JPEG, file_id="HI1")
    assert "जाँच के लिए भेज दिया गया" in last_text(out) and "वैकल्पिक" in last_text(out)
    assert "UTR सहेज लिया गया" in last_text(await tg.send(U1, "UTR000000077"))
    tg.clock.advance(3600)
    assert "पहले ही जमा हो चुका" in last_text(await tg.photo_bytes(U1, img.PNG, file_id="HI2"))
    assert tr("btn.premium", "hi") in [b.text for row in kb.main_menu("hi").keyboard for b in row]


async def test_premium_screen_lists_what_each_plan_gives(tg, shop, app_pool):
    await tg.onboard(U1)
    text = last_text(await tg.send(U1, "/premium"))
    assert "Free" in text and "100 random chats a day" in text  # the baseline to compare with
    assert "Premium 30 Days" in text and "₹99 / 30 days" in text
    assert "200 random chats a day" in text
    assert "See your partner's age, gender, city" in text
    assert text.index("Free") < text.index("Premium 30 Days") < text.index("Choose a plan")


async def test_plan_benefits_follow_admin_edits_and_the_tier(tg, shop, app_pool):
    big = await make_plan(
        app_pool, "vip_90", name="VIP 90 Days", days=90, price_minor=49900, tier="vip"
    )
    await tg.onboard(U1)
    out = await tg.send(U1, "/premium")
    text = last_text(out)
    assert "VIP 90 Days" in text and "Unlimited random chats" in text and "Profile boost" in text
    assert set(cbs(out)) == {f"pm:s:{shop}", f"pm:s:{big}", "pm:h"}
    await app_pool.execute(
        'UPDATE plan_entitlements SET value = \'{"enabled": true, "daily_limit": 321}\'::jsonb '
        "WHERE plan_id = $1 AND feature_key = 'random_chat'",
        shop,
    )
    assert "321 random chats a day" in last_text(await tg.send(U1, "/premium"))


async def test_premium_screen_in_hindi_lists_benefits(tg, shop, app_pool):
    await tg.onboard(U1)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", U1)
    text = last_text(await tg.send(U1, "/premium"))
    assert "रोज़ 200 रैंडम चैट" in text and "फ्री" in text


# ------------------------------------------------------------------ payment history (/payments)

REF_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


async def add_rejected(pool, uid, plan, n, reason="Amount did not match", prefix="AAAAAAAAA"):
    """n rejected payments for uid; the reference of the i-th is PAY-<prefix><letter i>."""
    refs = []
    for i in range(n):
        ref = f"PAY-{prefix}{REF_ALPHABET[i]}"
        refs.append(ref)
        await pool.execute(
            "INSERT INTO payments (reference, user_id, plan_id, amount_minor, currency, "
            "duration_days, pay_to, status, submitted_at, reviewed_by, reviewed_at, "
            "rejection_reason) VALUES ($1, $2, $3, 9900, 'INR', 30, 'shop@okbank', 'rejected', "
            "now(), 1, now(), $4)",
            ref,
            uid,
            plan,
            reason,
        )
    return refs


async def test_payments_command_and_button_show_history(tg, shop, app_pool):
    await tg.onboard(U1)
    out = await tg.send(U1, "/payments")
    assert "no payments yet" in last_text(out)
    out = await tg.send(U1, "/premium")
    assert "pm:h" in cbs(out)
    (ref,) = await add_rejected(app_pool, U1, shop, 1)
    for out in (await tg.send(U1, "/payments"), await tg.press(U1, "pm:h")):
        text = last_text(out)
        assert ref in text and "Premium 30 Days" in text and "₹99" in text
        assert "Not approved" in text and "Reason: Amount did not match" in text
        assert "Your payments" in text and "-20" in text  # a dd-mm-yyyy date


async def test_history_shows_a_pending_payment_and_the_premium_screen_keeps_its_button(
    tg, shop, app_pool
):
    await tg.onboard(U1)
    await open_order(tg, U1, shop)
    await tg.photo_bytes(U1, img.JPEG)
    ref = await app_pool.fetchval("SELECT reference FROM payments")
    text = last_text(await tg.press(U1, "pm:h"))
    assert ref in text and "Waiting for review" in text and "⏳" in text
    assert "Reason" not in text


async def test_history_is_capped_at_ten_newest_first_and_hides_unsubmitted_orders(
    tg, shop, app_pool
):
    await tg.onboard(U1)
    refs = await add_rejected(app_pool, U1, shop, 12)
    await tg.send(U1, "/premium")
    await open_order(tg, U1, shop)  # an order that was never submitted is not history
    ref_open = await app_pool.fetchval("SELECT reference FROM payments WHERE status = 'created'")
    text = last_text(await tg.send(U1, "/payments"))
    assert text.count("PAY-") == 10 and "latest 10" in text
    assert refs[11] in text and refs[2] in text  # the 10 newest ...
    assert refs[1] not in text and refs[0] not in text  # ... not the 2 oldest
    assert text.index(refs[11]) < text.index(refs[2])
    assert ref_open not in text


async def test_a_user_only_ever_sees_their_own_payments(tg, shop, app_pool):
    await tg.onboard(U1)
    await tg.onboard(U2)
    (mine,) = await add_rejected(app_pool, U1, shop, 1, reason="mine-reason", prefix="BBBBBBBBB")
    (theirs,) = await add_rejected(app_pool, U2, shop, 1, reason="their-reason")
    for out in (await tg.send(U2, "/payments"), await tg.press(U2, "pm:h")):
        text = last_text(out)
        assert theirs in text and "their-reason" in text
        assert mine not in text and "mine-reason" not in text
    # the button carries no payment id, so there is nothing to forge
    assert kb.parse_premium_cb("pm:h") == ("h", 0)
    assert kb.parse_premium_cb("pm:h:5") is None


async def test_history_is_rate_limited(tg, shop, app_pool):
    await tg.onboard(U1)
    await add_rejected(app_pool, U1, shop, 1)
    tg.auto_advance = 0.0  # no time passes between presses
    texts = [last_text(await tg.press(U1, "pm:h")) for _ in range(7)]
    assert "PAY-" in texts[0] and "Too many attempts" in texts[-1]


async def test_history_in_hindi_and_help_lists_the_command(tg, shop, app_pool):
    await tg.onboard(U1)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", U1)
    await add_rejected(app_pool, U1, shop, 1)
    text = last_text(await tg.send(U1, "/payments"))
    assert "आपके भुगतान" in text and "मंज़ूर नहीं हुआ" in text and "कारण: Amount" in text
    assert "/payments" in last_text(await tg.send(U1, "/help"))


async def test_a_lower_plan_is_refused_in_the_bot_with_a_clear_message(tg, app_pool):
    from tests.factories import give_tier

    await enable_payments(app_pool)
    premium = await make_plan(app_pool, "p_low", name="Low", tier="premium")
    await tg.onboard(U2)
    await give_tier(app_pool, U2, "vip")
    out = await tg.press(U2, f"pm:s:{premium}")
    text = last_text(out)
    assert "higher plan active" in text and "can't buy a lower plan" in text
    assert await app_pool.fetchval("SELECT count(*) FROM payments") == 0
