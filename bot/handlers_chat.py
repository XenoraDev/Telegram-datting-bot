import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from . import db, keyboards as kb

router = Router()
log = logging.getLogger(__name__)


async def _notify_ended(bot: Bot, uid: int, partner: int):
    """Tell both sides the chat ended and offer like/report/block."""
    await bot.send_message(
        uid, "Chat ended.", reply_markup=kb.main_menu
    )
    await bot.send_message(uid, "How was your partner?", reply_markup=kb.after_chat_kb(partner))
    try:
        await bot.send_message(partner, "😔 Your partner left the chat.", reply_markup=kb.main_menu)
        await bot.send_message(partner, "How was your partner?", reply_markup=kb.after_chat_kb(uid))
    except Exception:  # partner blocked the bot
        log.info("could not notify %s", partner)


async def start_search(m: Message, bot: Bot):
    uid = m.from_user.id
    if not await db.has_profile(uid):
        return await m.answer("Create your profile first: /start")
    u = await db.get_user(uid)
    if u["partner_id"]:
        return await m.answer("You are already in a chat. /stop to leave.", reply_markup=kb.chat_menu)
    partner = await db.join_or_match(uid)
    if partner is None:
        return await m.answer("🔎 Searching for a partner… /stop to cancel.", reply_markup=kb.chat_menu)
    me = await db.get_user(uid)
    # each side is told about the *other* person
    for target, who in ((uid, partner), (partner["id"], me)):
        text = (
            "🎉 <b>Partner found!</b>\n"
            f"They are {who['age']} · {kb.GENDER_LABEL[who['gender']]} · 📍 {who['city']}\n"
            "Say hi! Messages are anonymous. ⏭ Next or ⏹ Stop anytime."
        )
        try:
            await bot.send_message(target, text, reply_markup=kb.chat_menu)
        except Exception:
            log.info("could not notify %s", target)


@router.message(Command("chat"))
@router.message(F.text == kb.BTN_CHAT)
async def chat_cmd(m: Message, bot: Bot):
    await start_search(m, bot)


@router.message(Command("stop"))
@router.message(F.text == kb.BTN_STOP)
async def stop_cmd(m: Message, bot: Bot):
    uid = m.from_user.id
    partner = await db.end_chat(uid)
    if partner:
        return await _notify_ended(bot, uid, partner)
    if await db.leave_queue(uid):
        return await m.answer("Search cancelled.", reply_markup=kb.main_menu)
    await m.answer("You are not in a chat.", reply_markup=kb.main_menu)


@router.message(Command("next"))
@router.message(F.text == kb.BTN_NEXT)
async def next_cmd(m: Message, bot: Bot):
    uid = m.from_user.id
    partner = await db.end_chat(uid)
    if partner:
        await _notify_ended(bot, uid, partner)
    await db.leave_queue(uid)
    await start_search(m, bot)


# Relay everything else (text, photo, voice, stickers…) to the partner. Must be registered last.
@router.message(~F.text.startswith("/"))
async def relay(m: Message, bot: Bot):
    u = await db.get_user(m.from_user.id)
    if not u or not u["partner_id"]:
        return
    try:
        await bot.copy_message(u["partner_id"], m.chat.id, m.message_id)
    except Exception:
        await m.answer("⚠️ Could not deliver. Your partner may have left. Use ⏹ Stop.")
