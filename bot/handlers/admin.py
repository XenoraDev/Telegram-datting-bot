"""In-bot admin (P3): /admin dashboard, user lookup, reports queue, statistics.

Authorization: every handler (command, text input and EVERY callback) checks the Telegram id of
the update against ADMIN_IDS; callback payloads are only used to pick what to show, never to
decide who may. The service layer checks again before acting.
"""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import datetime

import asyncpg
from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import keyboards as kb
from ..config import Settings
from ..services import Services
from ..services.admin import REPORTS_PAGE, ActionResult, parse_user_id
from ..texts import en as t
from .common import notify_partner_left

log = logging.getLogger(__name__)


class AdminFlow(StatesGroup):
    user_id = State()


def _when(ts: datetime | None) -> str:
    return ts.strftime("%Y-%m-%d %H:%M UTC") if ts else "-"


# ---------------------------------------------------------------- rendering


def _home_text() -> str:
    return t.ADMIN_HOME


def _user_text(u: asyncpg.Record) -> str:
    lines = [
        f"👤 <b>User {u['id']}</b>",
        f"Status: <b>{t.esc(u['status'])}</b>"
        + (f" ({t.esc(u['status_reason'])})" if u["status_reason"] else ""),
        f"18+ confirmed: {'yes' if u['age_confirmed_at'] else 'no'}",
        f"Profile: {'yes' if u['has_profile'] else 'no'}",
    ]
    if u["has_profile"]:
        lines.append(
            f"{t.esc(u['name'])}, {u['age']} · {u['gender']} · {t.esc(u['country'] or '-')} · "
            f"{t.esc(u['city'])}"
        )
        lines.append(f"Bio: {t.esc(u['bio'])}")
    if u["username"]:
        lines.append(f"Username: @{t.esc(u['username'])}")
    lines += [
        f"Open reports against: {u['open_reports']} (total {u['total_reports']})",
        f"Reports filed: {u['reports_filed']} · blocked by {u['blocked_by']}",
        f"Restricted: {'yes' if u['restricted'] else 'no'} · in chat: "
        f"{'yes' if u['in_chat'] else 'no'}",
        f"Joined: {_when(u['created_at'])} · seen: {_when(u['last_seen_at'])}",
    ]
    return "\n".join(lines)


def _stats_text(s: asyncpg.Record) -> str:
    return (
        "📊 <b>Statistics</b>\n"
        f"Users: {s['total']}\n"
        f"  active {s['active']} · onboarding {s['onboarding']}\n"
        f"  banned {s['banned']} · underage-blocked {s['underage']}\n"
        f"Open reports: {s['open_reports']}\n"
        f"Active chats: {s['active_chats']} · queued: {s['queued']}\n"
        f"Matches: {s['matches']}"
    )


def _report_text(r: asyncpg.Record) -> str:
    lines = [
        f"🚨 <b>Report #{r['id']}</b> ({t.esc(r['status'])})",
        f"Reason: {t.esc(r['reason'])} · source: {t.esc(r['source'])}",
        f"Reporter: {r['reporter_id']}",
        f"Reported: {r['reported_id']} ({t.esc(r['reported_status'])}) · "
        f"open against: {r['open_against']}",
        f"Filed: {_when(r['created_at'])}",
    ]
    context = r["context"]
    if isinstance(context, str):
        context = json.loads(context)
    if context:
        lines.append("\n<b>Retained context</b> (RAM snapshot taken at report time):")
        for m in context:
            lines.append(f"• {t.esc(str(m.get('from', '?')))}: {t.esc(str(m.get('text', '')))}")
    else:
        lines.append("\nNo chat context retained.")
    return "\n".join(lines)[:3900]  # Telegram's limit is 4096 chars


# ---------------------------------------------------------------- plumbing


async def _show(target: Message, text: str, markup, *, edit: bool) -> None:
    """Edit the admin message in place (button navigation) or send a new one."""
    if edit:
        with contextlib.suppress(TelegramAPIError):  # e.g. "message is not modified"
            await target.edit_text(text, reply_markup=markup)
            return
    await target.answer(text, reply_markup=markup)


async def _show_home(target: Message, services: Services, uid: int, *, edit: bool) -> None:
    stats = await services.admin.stats(uid)
    open_reports = stats["open_reports"] if stats else 0
    await _show(target, _home_text(), kb.admin_home(open_reports), edit=edit)


async def _show_user(target: Message, services: Services, admin_id: int, uid: int, edit: bool):
    u = await services.admin.user(admin_id, uid)
    if u is None:
        return await _show(target, t.ADMIN_NO_USER, kb.admin_back(), edit=edit)
    markup = kb.admin_user(u["id"], u["status"], u["open_reports"])
    await _show(target, _user_text(u), markup, edit=edit)


async def _show_reports(target: Message, services: Services, admin_id: int, page: int, edit: bool):
    rows, total = await services.admin.reports_page(admin_id, page)
    if not rows:
        return await _show(target, t.ADMIN_NO_REPORTS, kb.admin_back(), edit=edit)
    pages = (total + REPORTS_PAGE - 1) // REPORTS_PAGE
    # reports_page clamps the page; recompute which one was served
    served = min(max(page, 0), pages - 1)
    items = [(r["id"], r["reason"], r["source"]) for r in rows]
    text = f"🚨 <b>Open reports</b> ({total}) · page {served + 1}/{pages}"
    await _show(target, text, kb.admin_reports(items, served, pages), edit=edit)


async def _show_report(target: Message, services: Services, admin_id: int, rid: int, edit: bool):
    r = await services.admin.report(admin_id, rid)
    if r is None:
        return await _show(target, t.ADMIN_NO_REPORT, kb.admin_back(), edit=edit)
    markup = kb.admin_report(r["id"], r["reported_id"], r["reported_status"])
    if r["status"] != "open":
        markup = kb.admin_back()
    await _show(target, _report_text(r), markup, edit=edit)


_RESULT_TEXT = {
    "denied": t.ADMIN_DENIED,
    "rate_limited": t.ADMIN_BUSY,
    "not_found": t.ADMIN_NO_USER,
    "invalid_state": t.ADMIN_BAD_STATE,
    "protected": t.ADMIN_PROTECTED,
    "already_handled": t.ADMIN_ALREADY,
}


# ---------------------------------------------------------------- commands + text input


async def admin_cmd(
    message: Message, settings: Settings, services: Services, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_ids:
        return await message.answer(t.ADMIN_DENIED)
    await state.clear()
    await _show_home(message, services, message.from_user.id, edit=False)


async def user_cmd(
    message: Message, command: CommandObject, settings: Settings, services: Services
) -> None:
    if message.from_user.id not in settings.admin_ids:
        return await message.answer(t.ADMIN_DENIED)
    uid = parse_user_id(command.args or "")
    if uid is None:
        return await message.answer(t.ADMIN_BAD_ID, reply_markup=kb.admin_back())
    await _show_user(message, services, message.from_user.id, uid, edit=False)


async def user_id_input(
    message: Message, settings: Settings, services: Services, state: FSMContext
) -> None:
    if message.from_user.id not in settings.admin_ids:
        await state.clear()
        return await message.answer(t.ADMIN_DENIED)
    uid = parse_user_id(message.text or "")
    if uid is None:
        return await message.answer(t.ADMIN_BAD_ID, reply_markup=kb.admin_back())
    await state.clear()
    await _show_user(message, services, message.from_user.id, uid, edit=False)


# ---------------------------------------------------------------- buttons


async def admin_cb(
    callback: CallbackQuery,
    settings: Settings,
    services: Services,
    state: FSMContext,
    bot: Bot,
) -> None:
    admin_id = callback.from_user.id
    if admin_id not in settings.admin_ids:  # never trust the payload for authorization
        await state.clear()
        return await callback.answer(t.ADMIN_DENIED, show_alert=True)
    parsed = kb.parse_admin_cb(callback.data)
    if parsed is None or not isinstance(callback.message, Message):
        return await callback.answer()
    verb, a, b = parsed
    msg = callback.message
    await callback.answer()

    if verb == "home":
        await state.clear()
        return await _show_home(msg, services, admin_id, edit=True)
    if verb == "find":
        await state.set_state(AdminFlow.user_id)
        return await _show(msg, t.ADMIN_ASK_ID, kb.admin_back(), edit=True)
    if verb == "st":
        stats = await services.admin.stats(admin_id)
        return await _show(msg, _stats_text(stats), kb.admin_back(), edit=True)
    if verb == "rl":
        return await _show_reports(msg, services, admin_id, a or 0, edit=True)
    if verb == "u" and a is not None:
        return await _show_user(msg, services, admin_id, a, edit=True)
    if verb == "r" and a is not None:
        return await _show_report(msg, services, admin_id, a, edit=True)
    if verb == "b" and a is not None:  # ask for confirmation first
        text = f"Ban user <b>{a}</b>? Their chat ends and they lose access."
        return await _show(msg, text, kb.admin_ban_confirm(a, b or 0), edit=True)
    if verb == "bo" and a is not None:
        res = await services.admin.ban(admin_id, a, report_id=b or None)
        await _after_action(msg, bot, res, t.ADMIN_BANNED)
        if res.kind == "ok":
            await _show_user(msg, services, admin_id, a, edit=True)
        return
    if verb == "ub" and a is not None:
        res = await services.admin.unban(admin_id, a)
        done = t.ADMIN_UNBANNED.format(status=res.detail)
        await _after_action(msg, bot, res, done)
        if res.kind == "ok":
            await _show_user(msg, services, admin_id, a, edit=True)
        return
    if verb in ("rv", "rd") and a is not None:
        res = await services.admin.close_report(admin_id, a, dismiss=verb == "rd")
        await _after_action(msg, bot, res, t.ADMIN_REPORT_DONE)
        if res.kind == "ok":
            await _show_reports(msg, services, admin_id, 0, edit=True)


async def _after_action(msg: Message, bot: Bot, res: ActionResult, ok_text: str) -> None:
    if res.kind == "ok":
        await msg.answer(ok_text)
        if res.partner_id is not None:
            await notify_partner_left(bot, res.partner_id)
    else:
        await msg.answer(_RESULT_TEXT.get(res.kind, t.ADMIN_BAD_STATE))


def build_router() -> Router:
    r = Router(name="admin")
    r.message.register(admin_cmd, Command("admin"))
    r.message.register(user_cmd, Command("user"))
    r.message.register(
        user_id_input, StateFilter(AdminFlow.user_id), F.text.regexp(r"^\s*[0-9]{1,30}\s*$")
    )
    r.callback_query.register(admin_cb, F.data.startswith("ad:"))
    return r
