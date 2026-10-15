"""P4 language foundation: default, selection, persistence, catalogs."""

import pytest

from bot.db import repo_users
from bot.services import build_services
from bot.texts import en, hi, i18n
from bot.texts.i18n import tr
from tests.factories import make_user


async def lang_in_db(pool, uid):
    return await pool.fetchval("SELECT language FROM users WHERE id = $1", uid)


async def test_default_language_is_english(app_pool, svc):
    await make_user(app_pool, 1)
    assert await lang_in_db(app_pool, 1) == "en"
    assert await svc.language.get(1) == "en"
    assert await svc.language.get(424242) == "en"  # unknown user -> default


async def test_select_hindi_then_english_again(app_pool, svc):
    await make_user(app_pool, 1)
    assert await svc.language.set(1, "hi") is True
    assert await lang_in_db(app_pool, 1) == "hi"
    assert await svc.language.get(1) == "hi"
    assert await svc.language.set(1, "en") is True
    assert await lang_in_db(app_pool, 1) == "en"


async def test_language_is_per_user(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await svc.language.set(1, "hi")
    assert await svc.language.get(2) == "en"


async def test_choice_survives_a_restart(app_pool, svc, clock):
    await make_user(app_pool, 1)
    await svc.language.set(1, "hi")
    restarted = build_services(app_pool, clock, clock)  # brand-new service graph, same database
    assert await restarted.language.get(1) == "hi"
    assert (await repo_users.get(app_pool, 1))["language"] == "hi"


@pytest.mark.parametrize("bad", ["", "fr", "EN", "hi ", "en;DROP", None, "xx"])
async def test_unsupported_codes_are_rejected_and_nothing_changes(app_pool, svc, bad):
    await make_user(app_pool, 1)
    assert await svc.language.set(1, bad) is False
    assert await lang_in_db(app_pool, 1) == "en"


async def test_database_itself_refuses_unsupported_languages(app_pool):
    await make_user(app_pool, 1)
    with pytest.raises(Exception, match="users_language_supported"):
        await app_pool.execute("UPDATE users SET language = 'fr' WHERE id = 1")


@pytest.mark.parametrize("status", ["banned", "blocked_underage", "deleted"])
async def test_accounts_that_cannot_use_the_bot_cannot_change_language(app_pool, svc, status):
    await make_user(app_pool, 1, status=status, with_profile=False)
    assert await svc.language.set(1, "hi") is False


async def test_deleteme_resets_the_language(app_pool, svc):
    await make_user(app_pool, 1)
    await svc.language.set(1, "hi")
    await svc.account.delete_account(1)
    assert await lang_in_db(app_pool, 1) == "en"


# ---------------- catalogs ----------------


def test_hindi_catalog_has_exactly_the_english_keys():
    assert set(hi.MESSAGES) == set(en.MESSAGES)
    assert all(hi.MESSAGES[k].strip() for k in hi.MESSAGES)


def test_lookup_falls_back_safely():
    assert tr("lang.changed", "en") == en.MESSAGES["lang.changed"]
    assert tr("lang.changed", "hi") == hi.MESSAGES["lang.changed"]
    assert tr("lang.changed", "xx") == en.MESSAGES["lang.changed"]
    assert tr("lang.changed", None) == en.MESSAGES["lang.changed"]
    with pytest.raises(KeyError):
        tr("no.such.key", "en")


def test_missing_hindi_key_falls_back_to_english(monkeypatch):
    monkeypatch.setitem(en.MESSAGES, "test.only", "Hello {name}")
    assert tr("test.only", "hi", name="Sam") == "Hello Sam"


def test_selector_offers_exactly_english_and_hindi():
    assert i18n.SUPPORTED == ("en", "hi") and i18n.DEFAULT == "en"
