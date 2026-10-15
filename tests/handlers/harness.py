"""A fake Telegram for driving the real Dispatcher (middlewares, routers, FSM, services, DB).

Nothing leaves the process: Bot.__call__ is replaced, every API method the bot would call is
recorded in `tg.out`, and `tg.inbox(uid)` shows what a given user would have received.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from unittest.mock import patch

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendSticker, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    MessageEntity,
    MessageOriginUser,
    PhotoSize,
    Sticker,
    Update,
    User,
)

from bot.config import Settings
from bot.main import build_dispatcher
from bot.services import Services, build_services

SETTINGS = Settings(bot_token="1:x", database_url="postgresql://u:p@h/d")


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class Out:
    method: TelegramMethod[Any]
    chat_id: int | None

    @property
    def text(self) -> str:
        return getattr(self.method, "text", None) or ""

    @property
    def kind(self) -> str:
        return type(self.method).__name__

    @property
    def markup(self):
        return getattr(self.method, "reply_markup", None)


class FakeTelegram:
    def __init__(self, pool, *, auto_advance: float = 2.0) -> None:
        self.clock = FakeClock()
        self.services: Services = build_services(pool, self.clock, self.clock)
        self.dp = build_dispatcher(pool, SETTINGS, self.services)
        self.bot = Bot("1:x")
        self.out: list[Out] = []
        self.auto_advance = auto_advance
        self.unreachable: set[int] = set()
        self._update_id = 0
        self._msg_id = 0
        self._patch = patch.object(Bot, "__call__", new=self._call)

    async def __aenter__(self) -> FakeTelegram:
        self._patch.start()
        return self

    async def __aexit__(self, *exc) -> None:
        self._patch.stop()
        await self.bot.session.close()

    # ---- Bot.__call__ replacement
    async def _call(self, method, *a, **kw):
        chat_id = getattr(method, "chat_id", None)
        if chat_id in self.unreachable and isinstance(method, (SendMessage, SendSticker)):
            raise TelegramForbiddenError(method, "Forbidden: bot was blocked by the user")
        self.out.append(Out(method, chat_id))
        if isinstance(method, (SendMessage, SendSticker)):
            self._msg_id += 1
            return Message(
                message_id=self._msg_id,
                date=datetime.now(),
                chat=Chat(id=chat_id, type="private"),
                text=getattr(method, "text", None),
            )
        return True

    # ---- helpers
    def _next(self) -> int:
        self._update_id += 1
        self._msg_id += 1
        return self._update_id

    def _tick(self) -> None:
        self.clock.advance(self.auto_advance)

    @staticmethod
    def _user(uid: int, username: str | None = None) -> User:
        return User(id=uid, is_bot=False, first_name=f"U{uid}", username=username)

    def _message(self, uid: int, **fields) -> Message:
        return Message(
            message_id=self._msg_id,
            date=datetime.now(),
            chat=Chat(id=uid, type="private"),
            from_user=self._user(uid, fields.pop("username", None)),
            **fields,
        )

    async def _feed(self, update: Update) -> list[Out]:
        before = len(self.out)
        await self.dp.feed_update(self.bot, update)
        return self.out[before:]

    async def send(self, uid: int, text: str, *, username: str | None = None, entities=None):
        self._tick()
        ents = list(entities or [])
        if text.startswith("/") and not ents:
            cmd_len = len(text.split()[0])
            ents = [MessageEntity(type="bot_command", offset=0, length=cmd_len)]
        msg = self._message(uid, text=text, entities=ents or None, username=username)
        return await self._feed(Update(update_id=self._next(), message=msg))

    async def sticker(self, uid: int, file_id: str = "STICKER1"):
        self._tick()
        st = Sticker(
            file_id=file_id,
            file_unique_id="u" + file_id,
            type="regular",
            width=512,
            height=512,
            is_animated=False,
            is_video=False,
        )
        msg = self._message(uid, sticker=st)
        return await self._feed(Update(update_id=self._next(), message=msg))

    async def photo(self, uid: int):
        self._tick()
        ph = [PhotoSize(file_id="P1", file_unique_id="up1", width=10, height=10)]
        msg = self._message(uid, photo=ph)
        return await self._feed(Update(update_id=self._next(), message=msg))

    async def forwarded(self, uid: int, text: str, from_uid: int = 999):
        self._tick()
        origin = MessageOriginUser(
            type="user", date=datetime.now(), sender_user=self._user(from_uid)
        )
        msg = self._message(uid, text=text, forward_origin=origin)
        return await self._feed(Update(update_id=self._next(), message=msg))

    async def press(self, uid: int, data: str):
        self._tick()
        cb = CallbackQuery(
            id=f"cb{self._next()}",
            from_user=self._user(uid),
            chat_instance="ci",
            data=data,
            message=self._message(uid, text="(button message)"),
        )
        return await self._feed(Update(update_id=self._next(), callback_query=cb))

    # ---- inspection
    def inbox(self, uid: int) -> list[Out]:
        return [
            o for o in self.out if o.chat_id == uid and o.kind in ("SendMessage", "SendSticker")
        ]

    def texts(self, uid: int) -> list[str]:
        return [o.text for o in self.inbox(uid) if o.kind == "SendMessage"]

    def callback_answers(self) -> list[AnswerCallbackQuery]:
        return [o.method for o in self.out if isinstance(o.method, AnswerCallbackQuery)]

    # ---- scenario shortcuts
    async def onboard(
        self,
        uid: int,
        *,
        name="Alex",
        age="25",
        gender="👨 Male",
        looking="👥 Everyone",
        city="Berlin",
        bio="Hello there",
    ):
        await self.send(uid, "/start")
        await self.press(uid, "age:yes")
        await self.send(uid, name)
        await self.send(uid, age)
        await self.send(uid, gender)
        await self.send(uid, looking)
        await self.send(uid, city)
        return await self.send(uid, bio)
