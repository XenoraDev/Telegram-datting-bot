import pytest

from bot.config import DEFAULT_HEARTBEAT_FILE, ConfigError, load_settings

VALID = {"BOT_TOKEN": "123:abc", "DATABASE_URL": "postgresql://u:p@db:5432/x"}


def test_minimal_valid_environment_uses_defaults():
    s = load_settings(VALID)
    assert s.bot_token == "123:abc"
    assert s.database_url == "postgresql://u:p@db:5432/x"
    assert s.admin_ids == frozenset()
    assert s.log_level == "INFO"
    assert s.heartbeat_file == DEFAULT_HEARTBEAT_FILE


@pytest.mark.parametrize("missing", ["BOT_TOKEN", "DATABASE_URL"])
def test_required_variables(missing):
    env = {k: v for k, v in VALID.items() if k != missing}
    with pytest.raises(ConfigError, match=missing):
        load_settings(env)


@pytest.mark.parametrize("name", ["BOT_TOKEN", "DATABASE_URL"])
def test_blank_required_variable_is_rejected(name):
    with pytest.raises(ConfigError, match=name):
        load_settings({**VALID, name: "   "})


def test_database_url_scheme_is_validated():
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_settings({**VALID, "DATABASE_URL": "sqlite:///bot.db"})
    assert load_settings({**VALID, "DATABASE_URL": "postgres://u@h/d"})


def test_admin_ids_parsing():
    s = load_settings({**VALID, "ADMIN_IDS": " 1, 22 ,,333 "})
    assert s.admin_ids == frozenset({1, 22, 333})


@pytest.mark.parametrize("bad", ["abc", "1,x", "-5", "1.5"])
def test_admin_ids_rejects_non_numeric(bad):
    with pytest.raises(ConfigError, match="ADMIN_IDS"):
        load_settings({**VALID, "ADMIN_IDS": bad})


def test_log_level_is_normalised_and_validated():
    assert load_settings({**VALID, "LOG_LEVEL": "debug"}).log_level == "DEBUG"
    assert load_settings({**VALID, "LOG_LEVEL": ""}).log_level == "INFO"
    with pytest.raises(ConfigError, match="LOG_LEVEL"):
        load_settings({**VALID, "LOG_LEVEL": "loud"})


def test_secrets_are_not_in_repr():
    text = repr(load_settings({**VALID, "BOT_TOKEN": "999:SECRETTOKEN"}))
    assert "SECRETTOKEN" not in text
    assert "u:p@db" not in text
