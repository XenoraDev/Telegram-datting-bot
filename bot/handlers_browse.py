from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InputMediaPhoto, Message

from . import config, db, keyboards as kb
from .handlers_profile import profile_text

router = Router()


def _contact(u) -> str:
    return f'<a href="tg://user?id={u["id"]}">{u["name"]}</a>' + (f' (@{u["username"]})' if u["username"] else "")


async def _send_next(bot: Bot, uid: int):
    u = await db.next_profile(uid)
    if not u:
        return await bot.send_message(
            uid, "No more profiles right now. Try again later or use 💬 Random Chat!", reply_markup=kb.main_menu
        )
    await bot.send_photo(uid, u["photo"], caption=profile_text(u), reply_markup=kb.swipe_kb(u["id"]))


@router.message(Command("browse"))
@router.message(F.text == kb.BTN_BROWSE)
async def browse(m: Message, bot: Bot):
    if not await db.has_profile(m.from_user.id):
        return await m.answer("Create your profile first: /start")
    await _send_next(bot, m.from_user.id)


async def _announce_match(bot: Bot, a: int, b: int):
    ua, ub = await db.get_user(a), await db.get_user(b)
    for target, other in ((a, ub), (b, ua)):
        try:
            await bot.send_photo(
                target, other["photo"],
                caption=f"💘 <b>It's a match!</b>\n\n{profile_text(other)}\n\nSay hi: {_contact(other)}",
            )
        except Exception:
            pass


@router.callback_query(F.data.startswith(("like:", "skip:")))
async def swipe_cb(c: CallbackQuery, bot: Bot):
    action, raw = c.data.split(":")
    target, me = int(raw), c.from_user.id
    from_browse = c.message.photo is not None and c.message.reply_markup and any(
        b.callback_data == f"skip:{target}" for row in c.message.reply_markup.inline_keyboard for b in row
    )
    liked = action == "like"
    if await db.get_user(target) is None:
        return await c.answer("User not found.", show_alert=True)
    matched = await db.swipe(me, target, liked)
    await c.message.edit_reply_markup(reply_markup=None)
    if matched:
        await c.answer("It's a match! 💘")
        await _announce_match(bot, me, target)
    else:
        await c.answer("Liked ❤️" if liked else "Skipped")
    if from_browse:
        await _send_next(bot, me)


@router.callback_query(F.data == "stopbrowse")
async def stop_browse(c: CallbackQuery):
    await c.message.edit_reply_markup(reply_markup=None)
    await c.message.answer("Stopped browsing.", reply_markup=kb.main_menu)
    await c.answer()


@router.callback_query(F.data.startswith("report:"))
async def report_cb(c: CallbackQuery, bot: Bot):
    target = int(c.data.split(":")[1])
    n = await db.report(c.from_user.id, target)
    await db.block(c.from_user.id, target)
    await db.swipe(c.from_user.id, target, False)
    if n >= config.REPORTS_BEFORE_AUTOBAN:
        await db.set_banned(target, True)
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Reported and blocked. Thank you.", show_alert=True)


@router.callback_query(F.data.startswith("block:"))
async def block_cb(c: CallbackQuery):
    await db.block(c.from_user.id, int(c.data.split(":")[1]))
    await c.message.edit_reply_markup(reply_markup=None)
    await c.answer("Blocked. You won't be matched again.", show_alert=True)


@router.message(Command("matches"))
@router.message(F.text == kb.BTN_MATCHES)
async def matches(m: Message):
    rows = await db.my_matches(m.from_user.id)
    if not rows:
        return await m.answer("No matches yet. Keep browsing ❤️")
    await m.answer("💕 <b>Your matches</b>\n\n" + "\n".join(f"• {_contact(u)}, {u['age']} — {u['city']}" for u in rows))
