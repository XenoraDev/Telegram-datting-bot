import pytest

from bot.loadtest.safety import (
    GROUPS,
    DbIdentity,
    LoadTestSettings,
    evaluate,
    looks_like_production,
)

ENV_ID = "env-abc12345"
GOOD = {
    "BOT_TOKEN": "7777777:TEST-token-not-real",
    "DATABASE_URL": "postgresql://tester:pw@db:5432/dating_loadtest",
    "ADMIN_IDS": "1",
    "APP_ENV": "testing",
    "LOAD_TEST_ENABLED": "1",
    "LOAD_TEST_ENV_ID": ENV_ID,
    "LOAD_TEST_DB_NAME": "dating_loadtest",
    "LOAD_TEST_BOT_ID": "7777777",
    "ADMIN_DOMAIN": "loadtest.example.com",
    "LOAD_TEST_ALLOWED_DOMAINS": "loadtest.example.com",
}
IDENTITY = DbIdentity("dating_loadtest", f"load-test-environment:{ENV_ID}", 0)


def report(env_over=None, identity=IDENTITY, drop=()):
    env = {k: v for k, v in GOOD.items() if k not in drop} | (env_over or {})
    return evaluate(LoadTestSettings.from_env(env), identity)


def failed(rep):
    return {c.key for c in rep.failed()}


def test_a_fully_declared_test_environment_is_green():
    rep = report()
    assert rep.ok and not rep.failed()
    assert all(rep.group_ok(g) for g in GROUPS)


@pytest.mark.parametrize("drop", ["APP_ENV", "LOAD_TEST_ENABLED"])
def test_a_missing_environment_flag_is_rejected(drop):
    assert "environment_flag" in failed(report(drop=[drop]))


@pytest.mark.parametrize("value", ["production", "prod", "live", "staging", "development", "dev"])
def test_a_production_or_unknown_environment_flag_is_rejected(value):
    rep = report({"APP_ENV": value})
    assert "environment_flag" in failed(rep) and not rep.ok


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:p@db:5432/dating_bot",  # the default production name: no 'test'
        "postgresql://u:p@db:5432/prod_test",  # contains test but also prod
        "postgresql://u:p@db-production.internal:5432/dating_loadtest",
        "postgresql://prod:p@db:5432/dating_loadtest",
        "postgresql://u:p@live.db.example:5432/dating_loadtest",
    ],
)
def test_a_production_looking_database_url_is_rejected(url):
    rep = report({"DATABASE_URL": url, "LOAD_TEST_DB_NAME": url.rsplit("/", 1)[1]})
    assert not rep.ok
    assert failed(rep) & {"database_not_production", "database_config"}


def test_the_declared_test_database_must_be_the_connected_one():
    rep = report({"LOAD_TEST_DB_NAME": "other_test"})
    assert "database_config" in failed(rep)
    assert "database_config" in failed(report(drop=["LOAD_TEST_DB_NAME"]))


def test_database_identity_must_match_the_live_database():
    assert "database_identity" in failed(report(identity=DbIdentity("dating_loadtest", "", 0)))
    assert "database_identity" in failed(
        report(identity=DbIdentity("dating_loadtest", "load-test-environment:someone-else", 0))
    )
    assert "database_identity" in failed(
        report(identity=DbIdentity("another_test", f"load-test-environment:{ENV_ID}", 0))
    )
    assert "database_identity" in failed(report(identity=None))
    assert "database_identity" in failed(report(drop=["LOAD_TEST_ENV_ID"]))


def test_unknown_negative_user_ids_mean_isolation_cannot_be_proved():
    rep = report(identity=DbIdentity("dating_loadtest", f"load-test-environment:{ENV_ID}", 3))
    assert failed(rep) == {"isolation"}


def test_the_bot_token_must_be_the_declared_test_bot_and_not_a_production_bot():
    assert "bot_credentials" in failed(report(drop=["LOAD_TEST_BOT_ID"]))
    assert "bot_credentials" in failed(report({"LOAD_TEST_BOT_ID": "1234567"}))
    assert "bot_credentials" in failed(report({"LOAD_TEST_PRODUCTION_BOT_IDS": "7777777"}))


def test_the_admin_domain_must_be_allow_listed_and_not_production():
    assert "domain" in failed(report(drop=["LOAD_TEST_ALLOWED_DOMAINS"]))
    assert "domain" in failed(report({"ADMIN_DOMAIN": "admin.example.com"}))
    both = {"LOAD_TEST_PRODUCTION_DOMAINS": "loadtest.example.com"}
    assert "domain" in failed(report(both))
    prodish = "prod.example.com"
    assert "domain" in failed(
        report({"ADMIN_DOMAIN": prodish, "LOAD_TEST_ALLOWED_DOMAINS": prodish})
    )


def test_an_unreadable_configuration_blocks():
    rep = report(drop=["BOT_TOKEN"])
    assert not rep.ok and "environment_flag" in failed(rep)


def test_localhost_is_not_a_test_environment():
    env = {"DATABASE_URL": "postgresql://u:p@localhost:5432/dating_bot", "APP_ENV": ""}
    assert not report(env).ok


def test_secrets_never_end_up_in_the_settings_or_the_report():
    cfg = LoadTestSettings.from_env(GOOD)
    dump = repr(cfg) + repr(report().as_json())
    assert "TEST-token" not in dump and ":pw@" not in dump and "pw" not in cfg.db_name
    assert cfg.bot_id == "7777777"


def test_production_word_detection_is_whole_word():
    assert looks_like_production("db-prod-1") and looks_like_production("live")
    assert not looks_like_production("dating_loadtest") and not looks_like_production("delivery")
