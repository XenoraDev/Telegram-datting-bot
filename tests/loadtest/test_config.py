import pytest

from bot.loadtest.config import PRESETS, Limits, config_hash, parse_config, preset_config

LIMITS = Limits()


def form(**kw):
    base = {
        "profile": "custom",
        "chats": "250",
        "msgs_per_chat_min": "50",
        "duration_min": "5",
        "ramp_s": "60",
    }
    return base | {k: str(v) for k, v in kw.items()}


def ok(**kw):
    cfg, errors = parse_config(form(**kw), LIMITS)
    assert cfg is not None, errors
    return cfg


def test_the_documented_example_numbers():
    cfg = ok(profile="heavy")
    assert cfg.profile == "heavy"
    assert cfg.total_msgs_per_min == 12_500
    assert round(cfg.msgs_per_s, 2) == 208.33
    assert cfg.full_load_messages == 62_500
    assert cfg.estimated_total_messages == 62_500 + 6_250  # plus half a ramp of average load
    assert cfg.total_runtime_s == 360


@pytest.mark.parametrize("name,expected", [(n, v) for n, v in PRESETS.items()])
def test_presets_are_workloads_with_the_documented_numbers(name, expected):
    cfg = preset_config(name)
    assert (cfg.chats, cfg.msgs_per_chat_min) == expected and cfg.profile == name
    assert PRESETS == {
        "light": (50, 40),
        "medium": (100, 40),
        "heavy": (250, 50),
        "stress": (500, 50),
    }


def test_a_preset_name_is_dropped_when_the_numbers_were_edited():
    assert ok(profile="heavy", chats=251).profile == "custom"
    assert ok(profile="light", chats=50, msgs_per_chat_min=40).profile == "light"


@pytest.mark.parametrize(
    "field,value",
    [
        ("chats", "0"),
        ("chats", "-5"),
        ("chats", "1.5"),
        ("chats", "abc"),
        ("chats", ""),
        ("chats", "2001"),
        ("chats", "99999999999"),
        ("msgs_per_chat_min", "0"),
        ("msgs_per_chat_min", "-1"),
        ("msgs_per_chat_min", "121"),
        ("duration_min", "0"),
        ("duration_min", "31"),
        ("duration_min", "five"),
        ("ramp_s", "-1"),
        ("ramp_s", "901"),
        ("ramp_s", "1e3"),
        ("profile", "nuclear"),
    ],
)
def test_dangerous_or_malformed_values_are_rejected_not_clamped(field, value):
    cfg, errors = parse_config(form(**{field: value}), LIMITS)
    assert cfg is None and errors, (field, value)


def test_total_rate_and_total_message_ceilings():
    _, errors = parse_config(form(chats=2000, msgs_per_chat_min=60), LIMITS)
    assert "msgs_per_chat_min" in errors  # 120,000 msg/min > 100,000
    tight = Limits(max_total_messages=10_000)
    cfg, errors = parse_config(form(), tight)
    assert cfg is None and "duration_min" in errors
    _, errors = parse_config(form(duration_min=1, ramp_s=90), LIMITS)
    assert "ramp_s" in errors  # ramp longer than the test


def test_limits_can_be_lowered_by_the_operator_but_have_a_hard_ceiling():
    lim = Limits.from_env({"LOAD_TEST_MAX_CHATS": "100", "LOAD_TEST_MAX_DURATION_S": "600"})
    assert lim.max_chats == 100 and lim.max_duration_s == 600
    assert Limits.from_env({"LOAD_TEST_MAX_CHATS": "999999"}).max_chats == 4000
    assert Limits.from_env({"LOAD_TEST_MAX_CHATS": "zero"}).max_chats == 2000
    _, errors = parse_config(form(chats=101), lim)
    assert "chats" in errors


def test_hash_is_canonical_and_changes_with_every_field():
    base = ok()
    assert base.digest == config_hash(dict(reversed(list(base.as_json().items()))))
    for change in (
        {"chats": 251},
        {"msgs_per_chat_min": 51},
        {"duration_min": 6},
        {"ramp_s": 61},
        {"profile": "heavy", "chats": 250},
    ):
        assert ok(**change).digest != base.digest, change
