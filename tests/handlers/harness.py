"""A fake Telegram for driving the real Dispatcher (middlewares, routers, FSM, services, DB).

Nothing leaves the process: Bot.__call__ is replaced, every API method the bot would call is
recorded in `tg.out`, and `tg.inbox(uid)` shows what a given user would have received.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from typing import Any
from unittest.mock import patch

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendSticker, TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
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

ADMIN_ID = 9000001  # the only admin in handler tests
SETTINGS = Settings(
    bot_token="1:x", database_url="postgresql://u:p@h/d", admin_ids=frozenset({ADMIN_ID})
)


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
    def __init__(self, pool, *, auto_advance: float = 10.0) -> None:
        self.clock = FakeClock()
        self.services: Services = build_services(pool, self.clock, self.clock, SETTINGS.admin_ids)
        self.dp = build_dispatcher(pool, SETTINGS, self.services)
        self.bot = Bot("1:x")
        self.out: list[Out] = []
        self.auto_advance = auto_advance
        self.unreachable: set[int] = set()
        self._update_id = 0
        self._msg_id = 0
        self._patch = patch.object(Bot, "__call__", new=self._call)
        self._patch_download = patch.object(Bot, "download", new=self._download)
        self.captions: dict[int, str] = {}  # uid -> caption for that user's next photo
        self.files: dict[str, bytes] = {}  # file_id -> bytes the fake Telegram can "download"

    async def __aenter__(self) -> FakeTelegram:
        self._patch.start()
        self._patch_download.start()
        return self

    async def __aexit__(self, *exc) -> None:
        self._patch_download.stop()
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

    async def _download(self, file, *args, **kwargs):
        data = self.files.get(file if isinstance(file, str) else getattr(file, "file_id", ""))
        if data is None:
            raise TelegramBadRequest(method=None, message="file is not available")  # type: ignore[arg-type]
        return BytesIO(data)

    # ---- helpers
    def _next(self) -> int:
        self._update_id += 1
        self._msg_id += 1
        return self._update_id

    def _tick(self) -> None:
        self.clock.advance(self.auto_advance)

    @staticmethod
    def _user(uid: int, username: str | None = None, is_bot: bool = False) -> User:
        return User(id=uid, is_bot=is_bot, first_name=f"U{uid}", username=username)

    def _message(self, uid: int, *, chat_type: str = "private", is_bot: bool = False, **fields):
        return Message(
            message_id=self._msg_id,
            date=datetime.now(),
            chat=Chat(id=uid if chat_type == "private" else -uid, type=chat_type),
            from_user=self._user(uid, fields.pop("username", None), is_bot),
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

    async def send_in_group(self, uid: int, text: str):
        self._tick()
        msg = self._message(uid, chat_type="supergroup", text=text)
        return await self._feed(Update(update_id=self._next(), message=msg))

    async def send_as_bot(self, uid: int, text: str):
        self._tick()
        msg = self._message(uid, is_bot=True, text=text)
        return await self._feed(Update(update_id=self._next(), message=msg))

    async def raw(self, uid: int, **fields):
        """Any other message type: voice=..., document=..., location=..., contact=..., dice=..."""
        self._tick()
        msg = self._message(uid, **fields)
        return await self._feed(Update(update_id=self._next(), message=msg))

    async def press_in_group(self, uid: int, data: str):
        self._tick()
        cb = CallbackQuery(
            id=f"cb{self._next()}",
            from_user=self._user(uid),
            chat_instance="ci",
            data=data,
            message=self._message(uid, chat_type="supergroup", text="x"),
        )
        return await self._feed(Update(update_id=self._next(), callback_query=cb))

    def callback_data(self, uid: int | None = None) -> list[str]:
        """Every callback_data string of every inline keyboard sent (optionally to one user)."""
        found: list[str] = []
        for o in self.out:
            if uid is not None and o.chat_id != uid:
                continue
            markup = o.markup
            for row in getattr(markup, "inline_keyboard", None) or []:
                found.extend(b.callback_data for b in row if b.callback_data)
        return found

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

    async def photo_bytes(self, uid: int, data: bytes, *, file_id: str = "SHOT1", declared=None):
        """A photo whose bytes the fake Telegram will serve when the bot downloads it."""
        self._tick()
        self.files[file_id] = data
        size = len(data) if declared is None else declared
        ph = [
            PhotoSize(
                file_id=file_id, file_unique_id="u" + file_id, width=10, height=10, file_size=size
            )
        ]
        msg = self._message(uid, photo=ph, caption=self.captions.pop(uid, None))
        return await self._feed(Update(update_id=self._next(), message=msg))

    async def document_bytes(
        self,
        uid: int,
        data: bytes,
        *,
        mime: str | None = "image/png",
        name="shot.png",
        file_id="DOC1",
    ):
        self._tick()
        self.files[file_id] = data
        doc = Document(
            file_id=file_id,
            file_unique_id="u" + file_id,
            file_name=name,
            mime_type=mime,
            file_size=len(data),
        )
        msg = self._message(uid, document=doc)
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
        country="🌍 Other",
        city="Berlin",
        bio="Hello there",
    ):
        await self.send(uid, "/start")
        await self.press(uid, "age:yes")
        await self.send(uid, name)
        await self.send(uid, age)
        await self.send(uid, gender)
        await self.send(uid, looking)
        await self.send(uid, country)
        await self.send(uid, city)
        return await self.send(uid, bio)
