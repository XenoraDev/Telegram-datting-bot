"""⭐ Premium: pick a plan, pay by UPI outside the bot, upload a screenshot, wait for an admin.

The screenshot is evidence only; this module never activates anything. Only the web admin's
approval does (PaymentService.approve). Callback data is pm:<verb>:<number> and every press is
re-checked server-side (ownership, state).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

import asyncpg
from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from .. import keyboards as kb
from ..config import Settings
from ..services import Services, benefits
from ..services.image_check import MAX_BYTES
from ..services.payments import StartResult
from ..services.upi import format_inr, qr_png
from ..telegram_send import deliver
from ..texts import en as t
from ..texts.i18n import tr
from .common import drop_buttons
from .filters import ActiveUser

log = logging.getLogger(__name__)

MAX_LIST_CHARS = 3000  # Telegram caps a message at 4096; leave room for the rest of the screen
UTR_WINDOW_SECONDS = 600  # how long after submitting a UTR message is taken as the UTR

_MENU_KEYS = (
    "btn.chat",
    "btn.browse",
    "btn.matches",
    "btn.profile",
    "btn.premium",
    "btn.language",
    "btn.support",
    "btn.next",
    "btn.stop",
    "btn.report",
)
MENU_TEXTS = frozenset().union(*(kb.labels(k) for k in _MENU_KEYS))
_ERROR_KEY = {
    "not_image": "premium.screenshot_not_image",
    "too_big": "premium.screenshot_too_big",
    "corrupt": "premium.screenshot_corrupt",
    "rate_limited": "premium.rate_limited",
    "no_order": "premium.no_order",
    "already_submitted": "premium.already_submitted",
    "duplicate_utr": "premium.utr_duplicate",
}


class PremiumFlow(StatesGroup):
    utr = State()  # after the screenshot was submitted: an optional UTR message may follow


def fmt_date(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%d-%m-%Y")


# ---------------------------------------------------------------- the Premium screen


async def _home(target: Message, services: Services, uid: int, lang: str) -> None:
    ov = await services.payments.overview(uid)
    lines = [tr("premium.header", lang)]
    if ov.premium_until is not None:
        lines.append(tr("premium.active", lang, date=fmt_date(ov.premium_until)))
    pending = (
        ov.open_payment if ov.open_payment and ov.open_payment["status"] == "pending" else None
    )
    markup = None
    if pending is not None:
        lines.append(_pending_text(pending, lang))
    else:
        if ov.last_rejected is not None:
            lines.append(
                tr(
                    "premium.rejected_note",
                    lang,
                    reference=ov.last_rejected["reference"],
                    reason=t.esc(ov.last_rejected["rejection_reason"]),
                )
            )
        if not ov.ready:
            lines.append(tr("premium.unavailable", lang))
        elif not ov.plans:
            lines.append(tr("premium.none", lang))
        else:
            lines.append(_plan_list(ov, lang))
            lines.append(tr("premium.choose", lang))
            markup = kb.premium_plans(
                [
                    (p["id"], p["name"], format_inr(p["price_minor"]), p["duration_days"])
                    for p in ov.plans
                ],
                lang,
            )
    if markup is None and pending is not None:
        markup = kb.payments_only(lang)
    await target.answer("\n\n".join(lines), reply_markup=markup)


def _plan_list(ov, lang: str) -> str:
    """Every plan with what it gives, under a one-line Free baseline for comparison."""
    free = benefits.effective(ov.free_features)
    parts = [tr("premium.free_head", lang) + "\n" + _bullets(benefits.lines(free, lang))]
    for p in ov.plans:
        eff = benefits.effective(ov.free_features, ov.plan_features.get(p["id"], {}))
        parts.append(
            benefits.plan_block(
                t.esc(p["name"]),
                format_inr(p["price_minor"]),
                p["duration_days"],
                benefits.lines(eff, lang),
                lang,
            )
        )
    text = "\n\n".join(parts)
    if len(text) > MAX_LIST_CHARS:  # many plans: keep the names and prices, drop the details
        text = "\n".join(
            tr(
                "premium.plan_head",
                lang,
                name=t.esc(p["name"]),
                amount=format_inr(p["price_minor"]),
                days=p["duration_days"],
            )
            for p in ov.plans
        )
    return text


async def plans_cta(services: Services, uid: int, lang: str) -> str | None:
    """The plan list with benefits as plain text (no buttons: a payment cannot start mid-chat),
    or None when Premium is not on sale."""
    ov = await services.payments.overview(uid)
    if not ov.ready or not ov.plans:
        return None
    return _plan_list(ov, lang) + "\n\n" + tr("pc.reveal_cta", lang)


def _bullets(items: list[str]) -> str:
    return "\n".join(f"• {i}" for i in items)


def _pending_text(payment: asyncpg.Record, lang: str) -> str:
    return tr(
        "premium.pending",
        lang,
        reference=payment["reference"],
        plan=t.esc(payment["plan_name"]),
        amount=format_inr(payment["amount_minor"]),
    )


async def premium_cmd(message: Message, services: Services, state: FSMContext, lang: str) -> None:
    await state.clear()
    if await services.matchmaking.current_chat(message.from_user.id):
        return await message.answer(tr("premium.in_chat", lang))
    await _home(message, services, message.from_user.id, lang)


# ---------------------------------------------------------------- payment history

_STATUS_ICON = {"pending": "⏳", "approved": "✅", "rejected": "❌"}


def history_text(rows: list[asyncpg.Record], lang: str) -> str:
    if not rows:
        return tr("payments.empty", lang)
    items = []
    for r in rows:
        item = tr(
            "payments.item",
            lang,
            icon=_STATUS_ICON[r["status"]],
            reference=r["reference"],
            plan=t.esc(r["plan_name"]),
            amount=format_inr(r["amount_minor"]),
            status=tr(f"payments.status_{r['status']}", lang),
            date=fmt_date(r["submitted_at"] or r["created_at"]),
        )
        if r["status"] == "rejected":
            item += "\n" + tr("payments.reason", lang, reason=t.esc(r["rejection_reason"]))
        items.append(item)
    return tr("payments.title", lang, n=len(rows)) + "\n\n" + "\n\n".join(items)


async def _history(target: Message, services: Services, uid: int, lang: str) -> None:
    rows = await services.payments.history(uid)
    if rows is None:
        return await target.answer(tr("premium.rate_limited", lang))
    await target.answer(history_text(rows, lang))


async def payments_cmd(message: Message, services: Services, state: FSMContext, lang: str) -> None:
    await state.clear()
    if await services.matchmaking.current_chat(message.from_user.id):
        return await message.answer(tr("premium.in_chat", lang))
    await _history(message, services, message.from_user.id, lang)


# ---------------------------------------------------------------- buttons


async def premium_cb(
    callback: CallbackQuery, services: Services, state: FSMContext, lang: str, settings: Settings
) -> None:
    parsed = kb.parse_premium_cb(callback.data)
    msg = callback.message if isinstance(callback.message, Message) else None
    if parsed is None or msg is None:
        return await callback.answer()
    verb, number = parsed
    uid = callback.from_user.id
    if await services.matchmaking.current_chat(uid):
        return await callback.answer(tr("premium.in_chat", lang), show_alert=True)
    if verb == "h":  # history of the presser's OWN payments; no id in the button to forge
        await callback.answer()
        return await _history(msg, services, uid, lang)
    if verb == "s":
        return await _choose_plan(callback, msg, services, number, lang)
    payment = await services.payments.order(uid, number)
    if payment is None or payment["status"] != "created":
        await state.clear()
        await drop_buttons(callback)
        return await callback.answer(tr("premium.stale", lang), show_alert=True)
    await callback.answer()
    if verb == "c":
        await services.payments.cancel(uid, number)
        await state.clear()
        await drop_buttons(callback)
        await msg.answer(tr("premium.cancelled", lang), reply_markup=kb.main_menu(lang))
    else:  # "u" / "k": buttons of older messages. No button is needed any more: just send the photo
        await msg.answer(tr("premium.ask_screenshot", lang))


async def _choose_plan(
    callback: CallbackQuery, msg: Message, services: Services, plan_id: int, lang: str
) -> None:
    res: StartResult = await services.payments.start(callback.from_user.id, plan_id)
    if res.kind != "ok":
        key = {
            "unavailable": "premium.unavailable",
            "plan_unavailable": "premium.none",
            "not_active": "finish_setup",
            "has_pending": None,
            "rate_limited": "premium.rate_limited",
            "busy": "premium.busy",
        }[res.kind]
        await callback.answer()
        await drop_buttons(callback)
        if res.kind == "has_pending":
            return await msg.answer(_pending_text(res.payment, lang))
        return await msg.answer(tr(key, lang))
    await callback.answer()
    await drop_buttons(callback)
    order = res.order
    await msg.answer_photo(
        BufferedInputFile(qr_png(order.uri), filename="pay.png"),
        caption=tr("premium.qr_caption", lang, amount=order.amount, reference=order.reference),
    )
    await msg.answer(
        tr(
            "premium.pay",
            lang,
            plan=t.esc(order.plan_name),
            days=order.duration_days,
            amount=order.amount,
            reference=order.reference,
            upi=t.esc(order.upi_id),
            payee=t.esc(order.payee_name),
            instructions=(t.esc(order.instructions) + "\n\n") if order.instructions else "",
        ),
        reply_markup=kb.payment_actions(order.payment_id, lang),
    )


# ---------------------------------------------------------------- conversation steps


async def leave_flow(message: Message, state: FSMContext) -> None:
    """A command or a menu button ends the payment conversation and is handled normally."""
    await state.clear()
    raise SkipHandler


async def got_photo(
    message: Message,
    bot: Bot,
    services: Services,
    state: FSMContext,
    lang: str,
    settings: Settings,
) -> None:
    """A photo (or image file) from an active user. If they have an open order it IS the evidence
    for that order, found from their Telegram id; without one nothing is created. A caption may
    carry the UTR. Files that are not images, and any media during a chat, are not ours."""
    uid = message.from_user.id
    if await services.matchmaking.current_chat(uid):
        raise SkipHandler
    if message.photo:
        file = message.photo[-1]
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file = message.document
    else:
        raise SkipHandler
    if (file.file_size or 0) > MAX_BYTES:
        return await message.answer(tr("premium.screenshot_too_big", lang))
    try:
        buf = await bot.download(file.file_id)
        data = buf.getvalue() if buf is not None else b""
    except TelegramAPIError:
        return await message.answer(tr("premium.screenshot_corrupt", lang))
    res = await services.payments.submit_screenshot(uid, data, message.caption)
    if res.kind != "ok":
        if res.kind == "no_order" and not services.limiter.allow("notice", uid):
            return
        return await message.answer(tr(_ERROR_KEY[res.kind], lang), reply_markup=None)
    has_utr = _has_utr(await services.payments.order(uid, res.payment_id))
    if has_utr:
        await state.clear()
    else:  # a UTR message is welcome for a few minutes
        await state.set_state(PremiumFlow.utr)
        await state.update_data(until=time.time() + UTR_WINDOW_SECONDS)
    key = "premium.submitted" if has_utr else "premium.submitted_ask_utr"
    await message.answer(tr(key, lang, reference=res.reference), reply_markup=kb.main_menu(lang))
    if res.reason == "utr_ignored":
        await message.answer(tr("premium.utr_invalid", lang))
    await _alert_admins(bot, services, settings, uid, res.payment_id)


def _has_utr(payment) -> bool:
    return bool(payment and payment["utr"])


async def got_utr(message: Message, services: Services, state: FSMContext, lang: str) -> None:
    """The optional UTR, sent after the screenshot. Applies to the sender's own order only."""
    uid = message.from_user.id
    until = (await state.get_data()).get("until")
    if await services.matchmaking.current_chat(uid) or not isinstance(until, float | int):
        await state.clear()
        raise SkipHandler
    if time.time() > until:  # too late to be an answer to our question
        await state.clear()
        raise SkipHandler
    res = await services.payments.add_utr(uid, message.text)
    if res.kind == "ok":
        await state.clear()
        return await message.answer(tr("premium.utr_saved", lang), reply_markup=kb.main_menu(lang))
    if res.kind in ("invalid_utr", "duplicate_utr", "rate_limited"):
        key = {
            "invalid_utr": "premium.utr_invalid",
            "duplicate_utr": "premium.utr_duplicate",
            "rate_limited": "premium.rate_limited",
        }[res.kind]
        return await message.answer(tr(key, lang))
    await state.clear()
    key = "premium.utr_already" if res.kind == "utr_already" else "premium.stale"
    await message.answer(tr(key, lang), reply_markup=kb.main_menu(lang))


async def _alert_admins(
    bot: Bot, services: Services, settings: Settings, uid: int, pid: int
) -> None:
    """Tell the admins a payment is waiting (they decide in the web panel, never here)."""
    payment = await services.payments.order(uid, pid)
    if payment is None:
        return
    text = t.ADMIN_PAYMENT_NEW.format(
        reference=payment["reference"],
        amount=format_inr(payment["amount_minor"]),
        plan=t.esc(payment["plan_name"]),
    )
    for admin_id in settings.admin_ids:
        await deliver(bot, admin_id, text, alerts=services.alerts)


def build_router() -> Router:
    r = Router(name="premium")
    active = ActiveUser()
    flow = StateFilter(PremiumFlow)
    leaves = F.text.startswith("/") | F.text.in_(MENU_TEXTS)
    r.message.register(premium_cmd, Command("premium"), active)
    r.message.register(premium_cmd, F.text.in_(kb.labels("btn.premium")), active)
    r.message.register(payments_cmd, Command("payments"), active)
    r.callback_query.register(premium_cb, F.data.startswith("pm:"), active)
    # A command or menu button ends the conversation and is then handled by its own router. The
    # steps below exclude those texts too, because FSM state is read once per update.
    r.message.register(leave_flow, flow, leaves)
    # A photo / image file from a user with no conversation open: evidence for their open order
    r.message.register(got_photo, StateFilter(None, PremiumFlow.utr), F.photo | F.document, active)
    r.message.register(got_utr, StateFilter(PremiumFlow.utr), ~leaves, F.text, active)
    return r
