"""The Telegram "/" command menu of the bot (what the client shows next to the message box).

Names and order follow /help (Discover, Chat & safety, Profile, Premium, Payments & support,
Information); Telegram's menu is a flat list, so each description starts with an emoji instead of a
heading. Descriptions are short and translated (`cmd.<name>` in the text catalogs).

Only public, user-facing commands are listed. Operator commands (/admin, /user), the health probe
(/ping), the sign-up helper (/cancel), /start and the old aliases (/matches, /likes) still work but
are deliberately not advertised.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats

from .texts.i18n import DEFAULT, SUPPORTED, tr

log = logging.getLogger(__name__)

# The order of /help, with /help itself first.
PUBLIC_COMMANDS: tuple[str, ...] = (
    "help",
    # discover
    "chat",
    "browse",
    "friends",
    "followers",
    "next",
    # chat & safety
    "stop",
    "report",
    "block",
    # profile
    "profile",
    "editprofile",
    "privacy",
    "language",
    # premium
    "premium",
    "myplan",
    "filters",
    "boost",
    # payments & support
    "payments",
    "support",
    "tickets",
    # information
    "terms",
    "about",
    "deleteme",
)


def build(lang: str) -> list[BotCommand]:
    return [BotCommand(command=c, description=tr(f"cmd.{c}", lang)) for c in PUBLIC_COMMANDS]


async def register(bot: Bot) -> None:
    """Publish the menu: English as the default, and one localized list per other language (the
    Telegram app picks it by its own language setting). A failure is logged, never fatal."""
    scope = BotCommandScopeAllPrivateChats()
    for lang in SUPPORTED:
        try:
            await bot.set_my_commands(
                build(lang), scope=scope, language_code=None if lang == DEFAULT else lang
            )
        except Exception:  # the menu is cosmetic: the bot must start without it
            log.exception("could not set the Telegram command menu (%s)", lang)
