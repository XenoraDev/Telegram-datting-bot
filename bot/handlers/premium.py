"""⭐ Premium: pick a plan, pay by UPI outside the bot, upload a screenshot, wait for an admin.

The screenshot is evidence only; this module never activates anything. Only the web admin's
approval does (PaymentService.approve). Callback data is pm:<verb>:<number> and every press is
re-checked server-side (ownership, state).
"""

from __future__ import annotations

import contextlib
import logging
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
from ..services import Services
from ..services.image_check import MAX_BYTES
from ..services.payments import StartResult
from ..services.upi import format_inr, qr_png
from ..texts import en as t
from ..texts.i18n import tr
from .common import drop_buttons
from .filters import ActiveUser

log = logging.getLogger(__name__)

_MENU_KEYS = (
    "btn.chat",
    "btn.browse",
    "btn.matches",
    "btn.profile",
    "btn.premium",
    "btn.language",
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
}


class PremiumFlow(StatesGroup):
    screenshot = State()
    utr = State()


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
            lines.append(tr("premium.choose", lang))
            markup = kb.premium_plans(
                [
                    (p["id"], p["name"], format_inr(p["price_minor"]), p["duration_days"])
                    for p in ov.plans
                ],
                lang,
            )
    await target.answer("\n\n".join(lines), reply_markup=markup)


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
    if verb == "s":
        return await _choose_plan(callback, msg, services, number, lang)
    payment = await services.payments.order(uid, number)
    if payment is None or payment["status"] != "created":
        await state.clear()
        await drop_buttons(callback)
        return await callback.answer(tr("premium.stale", lang), show_alert=True)
    await callback.answer()
    if verb == "u":
        await state.set_state(PremiumFlow.screenshot)
        await state.update_data(pid=number)
        await msg.answer(tr("premium.ask_screenshot", lang))
    elif verb == "c":
        await services.payments.cancel(uid, number)
        await state.clear()
        await drop_buttons(callback)
        await msg.answer(tr("premium.cancelled", lang), reply_markup=kb.main_menu(lang))
    elif verb == "k":
        await _submit(msg, callback.bot, services, state, uid, number, None, lang, settings)


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


async def _flow_pid(
    message: Message, services: Services, state: FSMContext, lang: str
) -> int | None:
    """The order this conversation belongs to, or None (and the user is told) if unusable."""
    uid = message.from_user.id
    if await services.matchmaking.current_chat(uid):
        await state.clear()
        await message.answer(tr("premium.in_chat", lang))
        return None
    pid = (await state.get_data()).get("pid")
    payment = await services.payments.order(uid, pid) if isinstance(pid, int) else None
    if payment is None or payment["status"] != "created":
        await state.clear()
        await message.answer(tr("premium.stale", lang), reply_markup=kb.main_menu(lang))
        return None
    return pid


async def got_screenshot(
    message: Message, bot: Bot, services: Services, state: FSMContext, lang: str
) -> None:
    pid = await _flow_pid(message, services, state, lang)
    if pid is None:
        return
    # Photos arrive as JPEG; documents are accepted only when they claim to be an image, and the
    # bytes are validated either way (the claim is never trusted).
    if message.photo:
        file = message.photo[-1]
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file = message.document
    else:
        return await message.answer(tr("premium.screenshot_not_image", lang))
    if (file.file_size or 0) > MAX_BYTES:
        return await message.answer(tr("premium.screenshot_too_big", lang))
    try:
        buf = await bot.download(file.file_id)
        data = buf.getvalue() if buf is not None else b""
    except TelegramAPIError:
        return await message.answer(tr("premium.screenshot_corrupt", lang))
    res = await services.payments.attach_evidence(message.from_user.id, pid, data)
    if res.kind == "ok":
        await state.set_state(PremiumFlow.utr)
        return await message.answer(
            tr("premium.ask_utr", lang), reply_markup=kb.skip_utr(pid, lang)
        )
    if res.kind == "invalid_state":
        await state.clear()
        return await message.answer(tr("premium.stale", lang), reply_markup=kb.main_menu(lang))
    await message.answer(tr(_ERROR_KEY[res.kind], lang))


async def screenshot_expected(
    message: Message, services: Services, state: FSMContext, lang: str
) -> None:
    if await _flow_pid(message, services, state, lang) is None:
        return
    if services.limiter.allow("notice", message.from_user.id):
        await message.answer(tr("premium.screenshot_expected", lang))


async def got_utr(
    message: Message,
    bot: Bot,
    services: Services,
    state: FSMContext,
    lang: str,
    settings: Settings,
) -> None:
    pid = await _flow_pid(message, services, state, lang)
    if pid is None:
        return
    await _submit(
        message, bot, services, state, message.from_user.id, pid, message.text, lang, settings
    )


async def _submit(
    target: Message,
    bot: Bot,
    services: Services,
    state: FSMContext,
    uid: int,
    pid: int,
    utr: str | None,
    lang: str,
    settings: Settings,
) -> None:
    res = await services.payments.submit(uid, pid, utr)
    if res.kind == "ok":
        await state.clear()
        await target.answer(
            tr("premium.submitted", lang, reference=res.reference), reply_markup=kb.main_menu(lang)
        )
        await _alert_admins(bot, services, settings, uid, pid)
        return
    if res.kind in ("invalid_utr", "duplicate_utr"):
        key = "premium.utr_invalid" if res.kind == "invalid_utr" else "premium.utr_duplicate"
        return await target.answer(tr(key, lang), reply_markup=kb.skip_utr(pid, lang))
    if res.kind == "no_evidence":
        await state.set_state(PremiumFlow.screenshot)
        await state.update_data(pid=pid)
        return await target.answer(tr("premium.ask_screenshot", lang))
    if res.kind == "rate_limited":
        return await target.answer(tr("premium.rate_limited", lang))
    await state.clear()
    await target.answer(tr("premium.stale", lang), reply_markup=kb.main_menu(lang))


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
        with contextlib.suppress(TelegramAPIError):
            await bot.send_message(admin_id, text)


def build_router() -> Router:
    r = Router(name="premium")
    active = ActiveUser()
    flow = StateFilter(PremiumFlow)
    leaves = F.text.startswith("/") | F.text.in_(MENU_TEXTS)
    r.message.register(premium_cmd, Command("premium"), active)
    r.message.register(premium_cmd, F.text.in_(kb.labels("btn.premium")), active)
    r.callback_query.register(premium_cb, F.data.startswith("pm:"), active)
    # A command or menu button ends the conversation and is then handled by its own router. The
    # steps below exclude those texts too, because FSM state is read once per update.
    r.message.register(leave_flow, flow, leaves)
    r.message.register(
        got_screenshot, StateFilter(PremiumFlow.screenshot), ~leaves, F.photo | F.document
    )
    r.message.register(screenshot_expected, StateFilter(PremiumFlow.screenshot), ~leaves)
    r.message.register(got_utr, StateFilter(PremiumFlow.utr), ~leaves, F.text)
    return r
