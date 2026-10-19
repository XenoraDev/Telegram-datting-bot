"""/help (categorized, English + Hindi), the Telegram "/" command menu and the main menu buttons.

UX only: no command was added, removed or renamed; every old alias still works."""

import re

import pytest
from aiogram.filters import Command

from bot import commands_menu
from bot import keyboards as kb
from bot.handlers import (
    account,
    admin,
    boost,
    browse,
    browse_filters,
    chat,
    language,
    likes,
    premium,
    privacy,
    profile,
    safety,
    start,
    support,
)
from bot.texts import en, hi
from bot.texts.i18n import tr

A = 5252001

# the required structure, in order: (heading, [commands])
STRUCTURE = [
    ("🧭", ["chat", "browse", "friends", "followers", "next"]),
    ("💬", ["stop", "report", "block"]),
    ("👤", ["profile", "editprofile", "privacy", "language"]),
    ("⭐", ["premium", "myplan", "filters", "boost"]),
    ("💳", ["payments", "support", "tickets"]),
    ("ℹ️", ["terms", "about", "deleteme"]),
]
EN_HEADINGS = ["DISCOVER", "CHAT &amp; SAFETY", "PROFILE", "PREMIUM", "PAYMENTS &amp; SUPPORT"]
EN_HEADINGS.append("INFORMATION")
HELP_COMMANDS = [c for _, cmds in STRUCTURE for c in cmds]

# registered commands that are deliberately NOT in /help and the menu
NOT_ADVERTISED = {
    "start",  # the Telegram Start button
    "ping",  # health probe
    "admin",
    "user",  # operator-only
    "cancel",  # only meaningful inside the sign-up conversation
    "matches",
    "likes",  # old names of /friends and /followers
}


def sections(text: str) -> list[tuple[str, list[str]]]:
    """[(heading line, [command names])] of a help text."""
    out: list[tuple[str, list[str]]] = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        cmds = [m.group(1) for ln in lines if (m := re.match(r"/(\w+) ", ln))]
        if cmds:
            out.append((lines[0], cmds))
    return out


def registered_commands() -> set[str]:
    modules = (
        account,
        admin,
        boost,
        browse,
        browse_filters,
        chat,
        language,
        likes,
        premium,
        privacy,
        profile,
        safety,
        start,
        support,
    )
    found: set[str] = set()
    for module in modules:
        for handler in module.build_router().message.handlers:
            for f in handler.filters:
                if isinstance(f.callback, Command):
                    found |= set(f.callback.commands)
    return found


# ---------------------------------------------------------------- /help


@pytest.mark.parametrize("lang", ["en", "hi"])
def test_help_is_split_into_the_six_categories_in_order(lang):
    text = tr("help", lang)
    got = sections(text)
    assert [cmds for _, cmds in got] == [cmds for _, cmds in STRUCTURE]
    for (heading, _), (icon, _) in zip(got, STRUCTURE, strict=True):
        assert heading.startswith(icon) and "<b>" in heading and "</b>" in heading
    assert text.count("<b>") == text.count("</b>") == 7  # six headings + the title
    assert text.startswith("📖 <b>")  # a title, then a blank line before every category
    assert text.count("\n\n") == 7  # title, 6 categories and the footer: 8 blocks


def test_help_english_headings_and_wording_are_the_requested_ones():
    text = tr("help", "en")
    for heading in EN_HEADINGS:
        assert f"<b>{heading}</b>" in text
    expected = {
        "chat": "Random Chat", "browse": "Discover people", "friends": "Your Friends",
        "followers": "People Following You", "next": "Next Person", "stop": "Leave Chat",
        "report": "Report Partner", "block": "Block Partner", "profile": "My Profile",
        "editprofile": "Edit Profile", "privacy": "Privacy Settings", "language": "Language",
        "premium": "Plans &amp; Benefits", "myplan": "My Plan",
        "filters": "Advanced Filters (Pro/VIP)", "boost": "Profile Boost (Pro/VIP)",
        "payments": "Payment History", "support": "Contact Support",
        "tickets": "My Support Tickets", "terms": "Terms &amp; Rules",
        "about": "About &amp; Privacy", "deleteme": "Delete Account",
    }  # fmt: skip
    for cmd, label in expected.items():
        assert f"/{cmd} — {label}\n" in text + "\n", cmd
    assert text.endswith(
        "💬 Chat supports text &amp; stickers.\n"
        "Links, emails and phone numbers are blocked for safety."
    )


def test_help_hindi_is_natural_hindi_with_the_same_commands_and_footer():
    text = tr("help", "hi")
    assert "<b>खोजें</b>" in text and "<b>चैट और सुरक्षा</b>" in text
    assert "<b>प्रोफ़ाइल</b>" in text and "<b>प्रीमियम</b>" in text
    assert "<b>भुगतान और सहायता</b>" in text and "<b>जानकारी</b>" in text
    assert text.endswith("लिंक, ई-मेल और फ़ोन नंबर सुरक्षा के लिए बंद हैं।")
    assert "टेक्स्ट और स्टिकर" in text
    assert not re.search(r"\b[A-Za-z]{3,}\b", re.sub(r"<[^>]+>|/\w+", " ", text))  # no English left
    assert re.findall(r"/(\w+) ", text) == re.findall(r"/(\w+) ", tr("help", "en"))


@pytest.mark.parametrize("lang", ["en", "hi"])
def test_help_is_valid_telegram_html(lang):
    text = tr("help", lang)
    assert not re.search(r"&(?!amp;|lt;|gt;)", text)  # a bare & breaks HTML parse mode
    assert "<" not in re.sub(r"</?b>", "", text) and ">" not in re.sub(r"</?b>", "", text)
    assert len(text) < 4096


def test_help_never_advertises_admin_internal_or_old_alias_commands():
    for lang in ("en", "hi"):
        shown = set(re.findall(r"(?m)^/(\w+) ", tr("help", lang)))
        assert shown == set(HELP_COMMANDS)
        assert shown.isdisjoint(NOT_ADVERTISED)


async def test_help_is_sent_in_the_users_language(tg, app_pool):
    await tg.onboard(A)
    assert (await tg.send(A, "/help"))[0].text == tr("help", "en")
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", A)
    assert (await tg.send(A, "/help"))[0].text == tr("help", "hi")


# ---------------------------------------------------------------- the Telegram command menu


@pytest.mark.parametrize("lang", ["en", "hi"])
def test_menu_has_the_help_commands_in_the_same_order_plus_help(lang):
    menu = [c.command for c in commands_menu.build(lang)]
    assert menu == ["help", *HELP_COMMANDS]
    assert menu == list(commands_menu.PUBLIC_COMMANDS)


@pytest.mark.parametrize("lang", ["en", "hi"])
def test_menu_descriptions_are_short_filled_and_valid_for_telegram(lang):
    for c in commands_menu.build(lang):
        assert re.fullmatch(r"[a-z0-9_]{1,32}", c.command)
        assert 3 <= len(c.description) <= 40, c  # short; Telegram allows up to 256
        assert c.description == c.description.strip() and "<" not in c.description
        assert not c.description[0].isascii()  # starts with an emoji
    assert len({c.description for c in commands_menu.build(lang)}) == 23  # all different


def test_menu_descriptions_exist_in_both_catalogs_and_hindi_is_hindi():
    for cmd in commands_menu.PUBLIC_COMMANDS:
        assert f"cmd.{cmd}" in en.MESSAGES and f"cmd.{cmd}" in hi.MESSAGES
        stripped = re.sub(r"\(Pro/VIP\)", "", en.MESSAGES[f"cmd.{cmd}"])
        assert stripped and hi.MESSAGES[f"cmd.{cmd}"] != en.MESSAGES[f"cmd.{cmd}"]
        assert not re.search(r"[A-Za-z]{2,}", hi.MESSAGES[f"cmd.{cmd}"])


def test_menu_has_no_admin_internal_or_alias_commands():
    menu = set(commands_menu.PUBLIC_COMMANDS)
    assert menu.isdisjoint(NOT_ADVERTISED)
    assert not any("admin" in c or "loadtest" in c or "debug" in c for c in menu)


def test_every_command_the_bot_has_is_either_public_or_deliberately_not_advertised():
    """A new command must be put in /help + the menu, or consciously listed as not advertised."""
    registered = registered_commands()
    public = set(commands_menu.PUBLIC_COMMANDS)
    assert public <= registered  # nothing in the menu is dead
    assert registered - public == NOT_ADVERTISED


async def test_register_publishes_english_by_default_and_hindi_separately():
    calls = []

    class FakeBot:
        async def set_my_commands(self, commands, scope=None, language_code=None):
            calls.append((commands, scope, language_code))

    await commands_menu.register(FakeBot())
    assert [c[2] for c in calls] == [None, "hi"]
    assert [c.description for c in calls[0][0]] == [
        tr(f"cmd.{x}", "en") for x in commands_menu.PUBLIC_COMMANDS
    ]
    assert [c.description for c in calls[1][0]] == [
        tr(f"cmd.{x}", "hi") for x in commands_menu.PUBLIC_COMMANDS
    ]
    assert all(type(c[1]).__name__ == "BotCommandScopeAllPrivateChats" for c in calls)


async def test_a_failing_menu_registration_never_stops_the_bot():
    class BrokenBot:
        async def set_my_commands(self, *a, **k):
            raise RuntimeError("telegram is down")

    await commands_menu.register(BrokenBot())  # logged, not raised


# ---------------------------------------------------------------- nothing regressed


async def test_old_aliases_still_open_the_same_screens(tg, app_pool):
    await tg.onboard(A)
    for new, old in (("/friends", "/matches"), ("/followers", "/likes")):
        a = [o.text for o in await tg.send(A, new)]
        b = [o.text for o in await tg.send(A, old)]
        assert a == b and a, (new, old)


@pytest.mark.parametrize("lang", ["en", "hi"])
async def test_every_main_menu_button_still_reaches_its_handler(tg, app_pool, lang):
    """The reply keyboard already has the six main entries (Random Chat, Browse, Friends,
    Profile, Premium, Support) plus Language; each one must open its screen, not the fallback."""
    await tg.onboard(A)
    await app_pool.execute("UPDATE users SET language = $2 WHERE id = $1", A, lang)
    labels = [b.text for row in kb.main_menu(lang).keyboard for b in row]
    assert labels == [
        tr(k, lang)
        for k in (
            "btn.chat",
            "btn.browse",
            "btn.matches",
            "btn.profile",
            "btn.premium",
            "btn.language",
            "btn.support",
        )
    ]
    for label in labels:
        await tg.send(A, "/stop")  # leave a chat/search that the previous button may have opened
        out = await tg.send(A, label)
        assert out and all(o.text != tr("unknown", lang) for o in out), label


def test_the_main_menu_labels_are_the_expected_ones():
    assert [b.text for r in kb.main_menu("en").keyboard for b in r] == [
        "💬 Random Chat",
        "🔎 Browse",
        "👥 My Friends",
        "👤 My Profile",
        "⭐ Premium",
        "🌐 Language",
        "🆘 Support",
    ]


def test_command_handlers_were_not_removed():
    registered = registered_commands()
    for cmd in (*HELP_COMMANDS, "help", "matches", "likes", "start", "ping", "admin", "user"):
        assert cmd in registered, cmd
