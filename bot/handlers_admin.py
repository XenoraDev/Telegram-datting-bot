from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from . import config, db

router = Router()


def _admin(m: Message) -> bool:
    return m.from_user.id in config.ADMIN_IDS


@router.message(Command("stats"))
async def stats(m: Message):
    if _admin(m):
        s = await db.stats()
        await m.answer(f"Users: {s['users']}\nWaiting: {s['waiting']}\nIn chat: {s['chatting']}")


@router.message(Command("ban", "unban"))
async def ban(m: Message, command: CommandObject):
    if not _admin(m):
        return
    if not command.args or not command.args.strip().isdigit():
        return await m.answer(f"Usage: /{command.command} <user_id>")
    uid = int(command.args)
    await db.set_banned(uid, command.command == "ban")
    await m.answer(f"{command.command}ned {uid}" if command.command == "ban" else f"unbanned {uid}")
