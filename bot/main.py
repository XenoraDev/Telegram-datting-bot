import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message

from . import config, db, handlers_admin, handlers_browse, handlers_chat, handlers_profile


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user:
            row = await db.get_user(user.id)
            if row and row["banned"]:
                if isinstance(event, Message):
                    await event.answer("🚫 You are banned.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("🚫 You are banned.", show_alert=True)
                return
        return await handler(event, data)


async def main():
    logging.basicConfig(level=logging.INFO)
    if not config.BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN in .env (get it from @BotFather)")
    await db.init()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(BanMiddleware())
    # order matters: relay (catch-all) must be last
    dp.include_routers(
        handlers_admin.router,
        handlers_profile.router,
        handlers_browse.router,
        handlers_chat.router,
    )
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
