"""🆘 Support: /support (category, then one message), /tickets (list, open, reply, close).

The user's text goes to support_messages and nowhere else; admins answer from the web panel and
the answer is delivered here by WebState.notify. Callback data is sp:<verb>:<own ticket id> and
every press is re-checked server-side (ownership, state).
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..config import Settings
from ..services import Services
from ..services.tickets import CATEGORIES, TicketResult
from ..telegram_send import deliver
from ..texts import en as t
from ..texts.i18n import tr
from .common import drop_buttons
from .filters import ActiveUser
from .premium import MENU_TEXTS

THREAD_CHARS = 3200  # a Telegram message holds 4096; leave room for the header
ONE_MESSAGE_CHARS = 700

_ERROR_KEY = {
    "rate_limited": "premium.rate_limited",
    "too_many": "support.too_many",
    "empty": "support.empty",
    "too_long": "support.too_long",
    "full": "support.full",
    "not_found": "support.not_found",
    "closed": "support.closed_already",
    "not_active": "finish_setup",
    "bad_category": "support.not_found",
}


class SupportFlow(StatesGroup):
    text = State()  # waiting for the first message of a new ticket (data: category)
    reply = State()  # waiting for a follow-up on an existing ticket (data: ticket_id)


def _clip(text: str) -> str:
    return text if len(text) <= ONE_MESSAGE_CHARS else text[:ONE_MESSAGE_CHARS] + "…"


def thread_text(view: dict, lang: str) -> str:
    """The ticket with its newest messages that fit one Telegram message, oldest first."""
    ticket = view["ticket"]
    parts: list[str] = []
    used = 0
    for m in reversed(view["messages"]):
        who = tr("support.from_user" if m["sender"] == "user" else "support.from_admin", lang)
        part = f"{who}: {t.esc(_clip(m['ticket_text']))}"
        if used + len(part) > THREAD_CHARS and parts:
            break
        parts.append(part)
        used += len(part)
    return tr(
        "support.view",
        lang,
        id=ticket["id"],
        category=tr(f"support.cat.{ticket['category']}", lang),
        status=tr(f"support.status_{ticket['status']}", lang),
        thread="\n\n".join(reversed(parts)),
    )


async def _alert_admins(bot: Bot, services: Services, settings: Settings, text: str) -> None:
    for admin_id in settings.admin_ids:
        await deliver(bot, admin_id, text, alerts=services.alerts)


# ---------------------------------------------------------------- commands


async def support_cmd(message: Message, services: Services, state: FSMContext, lang: str) -> None:
    await state.clear()
    if await services.matchmaking.current_chat(message.from_user.id):
        return await message.answer(tr("support.in_chat", lang))
    await message.answer(tr("support.choose", lang), reply_markup=kb.support_categories(lang))


async def tickets_cmd(message: Message, services: Services, state: FSMContext, lang: str) -> None:
    await state.clear()
    uid = message.from_user.id
    if await services.matchmaking.current_chat(uid):
        return await message.answer(tr("support.in_chat", lang))
    rows = await services.tickets.my_tickets(uid)
    if not rows:
        return await message.answer(tr("support.none", lang))
    await message.answer(
        tr("support.list_header", lang, n=len(rows)),
        reply_markup=kb.ticket_list([(r["id"], r["category"], r["status"]) for r in rows], lang),
    )


# ---------------------------------------------------------------- buttons


async def support_cb(
    callback: CallbackQuery, services: Services, state: FSMContext, lang: str
) -> None:
    parsed = kb.parse_support_cb(callback.data)
    msg = callback.message if isinstance(callback.message, Message) else None
    if parsed is None or msg is None:
        return await callback.answer()
    verb, arg = parsed
    uid = callback.from_user.id
    if await services.matchmaking.current_chat(uid):
        return await callback.answer(tr("support.in_chat", lang), show_alert=True)
    if verb == "x":
        await state.clear()
        await drop_buttons(callback)
        await callback.answer()
        return await msg.answer(tr("support.cancelled", lang), reply_markup=kb.main_menu(lang))
    if verb == "c":
        await callback.answer()
        await drop_buttons(callback)
        await state.set_state(SupportFlow.text)
        await state.update_data(category=arg)
        return await msg.answer(tr("support.ask", lang), reply_markup=kb.support_cancel(lang))
    view = await services.tickets.my_ticket(uid, arg)
    if view is None:  # not theirs, or gone (e.g. after /deleteme): same answer either way
        return await callback.answer(tr("support.not_found", lang), show_alert=True)
    closed = view["ticket"]["status"] == "closed"
    if verb == "v":
        await callback.answer()
        return await msg.answer(
            thread_text(view, lang), reply_markup=kb.ticket_actions(arg, closed, lang)
        )
    if closed:
        await drop_buttons(callback)
        return await callback.answer(tr("support.closed_already", lang), show_alert=True)
    await callback.answer()
    if verb == "r":
        await state.set_state(SupportFlow.reply)
        await state.update_data(ticket_id=arg)
        return await msg.answer(tr("support.ask_reply", lang), reply_markup=kb.support_cancel(lang))
    res = await services.tickets.close_by_user(uid, arg)  # verb "k"
    await drop_buttons(callback)
    key = "support.closed_ok" if res.kind == "ok" else _ERROR_KEY.get(res.kind, "support.not_found")
    await state.clear()
    await msg.answer(tr(key, lang, id=arg), reply_markup=kb.main_menu(lang))


# ---------------------------------------------------------------- conversation steps


async def leave_flow(message: Message, state: FSMContext) -> None:
    """A command or a menu button ends the support conversation and is handled normally."""
    await state.clear()
    raise SkipHandler


async def got_first_message(
    message: Message,
    bot: Bot,
    services: Services,
    state: FSMContext,
    lang: str,
    settings: Settings,
) -> None:
    uid = message.from_user.id
    category = (await state.get_data()).get("category")
    if category not in CATEGORIES:
        await state.clear()
        return await message.answer(tr("support.not_found", lang), reply_markup=kb.main_menu(lang))
    res = await services.tickets.open(uid, category, message.text)
    if res.kind == "ok":
        alert = t.ADMIN_TICKET_NEW.format(id=res.ticket_id, category=category)
        await state.clear()
        await message.answer(
            tr("support.created", lang, id=res.ticket_id), reply_markup=kb.main_menu(lang)
        )
        return await _alert_admins(bot, services, settings, alert)
    await _failed(message, res, state, lang)


async def got_followup(
    message: Message,
    bot: Bot,
    services: Services,
    state: FSMContext,
    lang: str,
    settings: Settings,
) -> None:
    ticket_id = (await state.get_data()).get("ticket_id")
    if not isinstance(ticket_id, int):
        await state.clear()
        return await message.answer(tr("support.not_found", lang), reply_markup=kb.main_menu(lang))
    res = await services.tickets.user_reply(message.from_user.id, ticket_id, message.text)
    if res.kind == "ok":
        await state.clear()
        await message.answer(tr("support.reply_sent", lang), reply_markup=kb.main_menu(lang))
        return await _alert_admins(
            bot, services, settings, t.ADMIN_TICKET_REPLY.format(id=ticket_id)
        )
    await _failed(message, res, state, lang)


async def _failed(message: Message, res: TicketResult, state: FSMContext, lang: str) -> None:
    if res.kind in ("empty", "too_long"):  # let them fix the text; the state stays
        return await message.answer(tr(_ERROR_KEY[res.kind], lang))
    await state.clear()
    key = _ERROR_KEY.get(res.kind, "support.not_found")
    await message.answer(tr(key, lang), reply_markup=kb.main_menu(lang))


async def not_text(message: Message, lang: str) -> None:
    """A photo, sticker or file while waiting for the ticket text."""
    await message.answer(tr("support.empty", lang))


def build_router() -> Router:
    r = Router(name="support")
    active = ActiveUser()
    flow = StateFilter(SupportFlow)
    leaves = F.text.startswith("/") | F.text.in_(MENU_TEXTS)
    r.message.register(support_cmd, Command("support"), active)
    r.message.register(support_cmd, F.text.in_(kb.labels("btn.support")), active)
    r.message.register(tickets_cmd, Command("tickets"), active)
    r.callback_query.register(support_cb, F.data.startswith("sp:"), active)
    # A command or menu button ends the conversation and is handled by its own router.
    r.message.register(leave_flow, flow, leaves)
    r.message.register(got_first_message, StateFilter(SupportFlow.text), ~leaves, F.text, active)
    r.message.register(got_followup, StateFilter(SupportFlow.reply), ~leaves, F.text, active)
    r.message.register(not_text, flow, ~F.text, active)
    return r
