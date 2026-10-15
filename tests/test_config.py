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


# ---------- DATABASE_URL built from POSTGRES_* parts ----------

PARTS = {
    "BOT_TOKEN": "123:abc",
    "POSTGRES_USER": "dating_bot",
    "POSTGRES_PASSWORD": "pw",
    "POSTGRES_DB": "dating_bot",
}


def test_database_url_is_built_from_parts_with_defaults():
    s = load_settings(PARTS)
    assert s.database_url == "postgresql://dating_bot:pw@db:5432/dating_bot"


def test_database_url_parts_honour_host_and_port():
    s = load_settings({**PARTS, "POSTGRES_HOST": "127.0.0.1", "POSTGRES_PORT": "5433"})
    assert s.database_url == "postgresql://dating_bot:pw@127.0.0.1:5433/dating_bot"


@pytest.mark.parametrize(
    "password",
    ["Sup3rSecret/xyz789", "a?b#c", "p@ss:word", "100%41", "sp ace", "[x]", "üñí", "a&b=c+d"],
)
def test_any_password_survives_url_building(password):
    from urllib.parse import unquote, urlsplit

    url = load_settings({**PARTS, "POSTGRES_PASSWORD": password}).database_url
    parsed = urlsplit(url)
    assert parsed.hostname == "db" and parsed.port == 5432
    assert unquote(parsed.password) == password
    assert unquote(parsed.username) == "dating_bot"
    assert parsed.path == "/dating_bot"


def test_explicit_database_url_wins_over_parts():
    s = load_settings({**PARTS, "DATABASE_URL": "postgresql://x:y@h:1/z"})
    assert s.database_url == "postgresql://x:y@h:1/z"


def test_blank_database_url_falls_back_to_parts():
    # docker compose sets DATABASE_URL="" on purpose
    assert load_settings({**PARTS, "DATABASE_URL": ""}).database_url.endswith("@db:5432/dating_bot")


def test_missing_parts_are_named_without_leaking_values():
    env = {"BOT_TOKEN": "123:abc", "POSTGRES_USER": "u", "POSTGRES_PASSWORD": "topsecret"}
    with pytest.raises(ConfigError, match="POSTGRES_DB") as exc:
        load_settings(env)
    assert "topsecret" not in str(exc.value)


@pytest.mark.parametrize(
    "url",
    ["postgresql://u:Sup3rSecret/xyz@db:5432/d", "postgresql://u:p?x@db:5432/d", "postgresql://"],
)
def test_malformed_database_url_is_a_config_error_that_does_not_echo_it(url):
    with pytest.raises(ConfigError, match="not a valid URL") as exc:
        load_settings({"BOT_TOKEN": "123:abc", "DATABASE_URL": url})
    assert "Sup3rSecret" not in str(exc.value)
