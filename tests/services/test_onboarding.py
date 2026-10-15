"""18+ gate, validation and server-side enforcement (service + SQL + CHECK constraints)."""

import asyncpg
import pytest

from bot import domain
from bot.db import repo_profiles, repo_users
from bot.services.onboarding import (
    InvalidInput,
    parse_age,
    validate_bio,
    validate_city,
    validate_gender,
    validate_looking_for,
    validate_name,
)

GOOD = dict(
    name="Alex",
    age=25,
    gender="m",
    looking_for="any",
    city="Berlin",
    bio="Hello there",
    country="ZZ",
)


# ---------------- pure validation ----------------


@pytest.mark.parametrize("raw,expected", [("18", 18), (" 25 ", 25), ("80", 80), ("007", 7)])
def test_parse_age_accepts_numbers(raw, expected):
    assert parse_age(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "abc", "twenty", "-5", "1.5", "25 years", "1e1", "1000", "２５５５"]
)
def test_parse_age_rejects_non_numbers(raw):
    with pytest.raises(InvalidInput) as e:
        parse_age(raw)
    assert e.value.code == "age_not_number"


@pytest.mark.parametrize("raw", ["81", "99", "120"])
def test_parse_age_rejects_unrealistic_ages(raw):
    with pytest.raises(InvalidInput) as e:
        parse_age(raw)
    assert e.value.code == "age_range"


def test_parse_age_understands_other_digit_scripts_so_they_cannot_bypass_the_gate():
    assert parse_age("١٧") == 17  # Arabic-Indic 17 -> still seen as a minor
    assert parse_age("２５") == 25  # fullwidth


@pytest.mark.parametrize("raw", ["", "   ", "x" * 31, "a\x00b", "a\x07b"])
def test_name_rejects_bad_values(raw):
    with pytest.raises(InvalidInput):
        validate_name(raw)


def test_name_is_trimmed_and_whitespace_collapsed():
    assert validate_name("  Anna   Maria \n") == "Anna Maria"
    assert validate_name("line\u2028sep") == "line sep"  # exotic separators become spaces


@pytest.mark.parametrize(
    "fn,raw,code",
    [
        (validate_name, "see foo.com", "text_link"),
        (validate_name, "@someone", "text_contact"),
        (validate_city, "Berlin t.me/x", "text_link"),
        (validate_bio, "call me 9876543210", "text_contact"),
        (validate_bio, "I'm 15 years old", "text_minor"),
        (validate_bio, "send nudes", "text_sexual"),
        (validate_city, "", "city_length"),
        (validate_city, "c" * 41, "city_length"),
        (validate_bio, "", "bio_length"),
        (validate_bio, "b" * 301, "bio_length"),
    ],
)
def test_text_fields_reject_links_contacts_minors_and_sexual_content(fn, raw, code):
    with pytest.raises(InvalidInput) as e:
        fn(raw)
    assert e.value.code == code


def test_bio_keeps_newlines_but_rejects_control_characters():
    assert validate_bio("line1\nline2") == "line1\nline2"
    with pytest.raises(InvalidInput):
        validate_bio("bad\x07bell")


def test_gender_and_looking_for_are_whitelists():
    assert validate_gender("f") == "f" and validate_looking_for("any") == "any"
    for bad in ("x", "male", "", "M"):
        with pytest.raises(InvalidInput):
            validate_gender(bad)
    with pytest.raises(InvalidInput):
        validate_looking_for("o")


# ---------------- /start state machine ----------------


async def test_new_user_must_confirm_age_first(app_pool, svc):
    assert (await svc.onboarding.start(1, "bob")).state == "need_age_confirmation"
    row = await repo_users.get(app_pool, 1)
    assert row["status"] == "onboarding" and row["age_confirmed_at"] is None
    assert row["username"] == "bob"


async def test_confirming_adult_moves_to_profile_step(app_pool, svc):
    await svc.onboarding.start(1, None)
    assert await svc.onboarding.confirm_adult(1) is True
    assert (await repo_users.get(app_pool, 1))["age_confirmed_at"] is not None
    assert (await svc.onboarding.start(1, None)).state == "need_profile"


async def test_confirm_adult_is_idempotent_and_keeps_first_timestamp(app_pool, svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    first = (await repo_users.get(app_pool, 1))["age_confirmed_at"]
    assert await svc.onboarding.confirm_adult(1) is True
    assert (await repo_users.get(app_pool, 1))["age_confirmed_at"] == first


async def test_unknown_user_cannot_confirm(svc):
    assert await svc.onboarding.confirm_adult(404) is False


async def test_self_declared_minor_is_hard_blocked_and_tombstoned(app_pool, svc):
    await svc.onboarding.start(1, "kid")
    await svc.onboarding.declare_minor(1)
    row = await repo_users.get(app_pool, 1)
    assert row["status"] == "blocked_underage" and row["username"] is None
    assert await repo_users.tombstone_reason(app_pool, 1) == "underage"
    assert await repo_profiles.get(app_pool, 1) is None
    assert (await svc.onboarding.start(1, "kid")).state == "blocked"


async def test_minor_cannot_unblock_by_confirming_adult_afterwards(app_pool, svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.declare_minor(1)
    assert await svc.onboarding.confirm_adult(1) is False  # only possible while onboarding
    assert (await repo_users.get(app_pool, 1))["status"] == "blocked_underage"


@pytest.mark.parametrize("age", ["17", "5", "0", "١٧"])
async def test_typing_an_age_under_18_hard_blocks(app_pool, svc, age):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    result = await svc.onboarding.submit_age(1, age)
    assert result.kind == "underage"
    assert (await repo_users.get(app_pool, 1))["status"] == "blocked_underage"
    assert await repo_users.tombstone_reason(app_pool, 1) == "underage"


async def test_exactly_18_is_allowed(svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    result = await svc.onboarding.submit_age(1, "18")
    assert (result.kind, result.age) == ("ok", 18)


async def test_invalid_age_does_not_block(app_pool, svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    with pytest.raises(InvalidInput):
        await svc.onboarding.submit_age(1, "abc")
    assert (await repo_users.get(app_pool, 1))["status"] == "onboarding"


# ---------------- completing a profile (server-side enforcement) ----------------


async def test_complete_profile_activates_the_user(app_pool, svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    assert (await svc.onboarding.complete_profile(1, **GOOD)).kind == "ok"
    assert (await repo_users.get(app_pool, 1))["status"] == "active"
    assert (await repo_profiles.get(app_pool, 1))["city"] == "Berlin"
    assert (await svc.onboarding.start(1, None)).state == "ready"


async def test_complete_profile_without_age_confirmation_is_refused_by_the_database(app_pool, svc):
    await svc.onboarding.start(1, None)  # NOT confirmed
    assert (await svc.onboarding.complete_profile(1, **GOOD)).kind == "not_allowed"
    assert await repo_profiles.get(app_pool, 1) is None
    assert (await repo_users.get(app_pool, 1))["status"] == "onboarding"


async def test_tampered_underage_value_in_complete_profile_blocks_the_user(app_pool, svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    result = await svc.onboarding.complete_profile(1, **{**GOOD, "age": 16})
    assert result.kind == "underage"
    assert (await repo_users.get(app_pool, 1))["status"] == "blocked_underage"
    assert await repo_profiles.get(app_pool, 1) is None


@pytest.mark.parametrize(
    "override",
    [
        {"gender": "x"},
        {"looking_for": "robots"},
        {"name": ""},
        {"city": "c" * 99},
        {"bio": "see http://x.com"},
        {"age": 200},
    ],
)
async def test_complete_profile_revalidates_every_field(app_pool, svc, override):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    with pytest.raises(InvalidInput):
        await svc.onboarding.complete_profile(1, **{**GOOD, **override})
    assert await repo_profiles.get(app_pool, 1) is None


async def test_blocked_user_cannot_create_a_profile(app_pool, svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = 1")
    assert (await svc.onboarding.complete_profile(1, **GOOD)).kind == "not_allowed"
    assert await repo_profiles.get(app_pool, 1) is None


async def test_editing_an_active_profile_keeps_user_active(app_pool, svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.confirm_adult(1)
    await svc.onboarding.complete_profile(1, **GOOD)
    await svc.onboarding.complete_profile(1, **{**GOOD, "city": "Paris"})
    assert (await repo_profiles.get(app_pool, 1))["city"] == "Paris"
    assert (await repo_users.get(app_pool, 1))["status"] == "active"


async def test_tombstoned_id_stays_blocked_even_if_users_row_is_reset(app_pool, svc):
    await svc.onboarding.start(1, None)
    await svc.onboarding.declare_minor(1)
    await app_pool.execute(
        "UPDATE users SET status = 'onboarding', status_reason = NULL WHERE id = 1"
    )
    assert (await svc.onboarding.start(1, None)).state == "blocked"
    assert (await repo_users.get(app_pool, 1))["status"] == "blocked_underage"  # re-blocked


# ---------------- database-level 18+ guarantees ----------------


async def test_db_refuses_a_profile_for_a_minor(app_pool):
    await app_pool.execute("INSERT INTO users (id) VALUES (1)")
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute(
            "INSERT INTO profiles (user_id, name, age, gender, looking_for, city, bio) "
            "VALUES (1, 'x', 17, 'm', 'any', 'c', 'b')"
        )


async def test_db_refuses_active_status_without_age_confirmation(app_pool):
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("INSERT INTO users (id, status) VALUES (1, 'active')")
    await app_pool.execute("INSERT INTO users (id) VALUES (2)")
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("UPDATE users SET status = 'active' WHERE id = 2")


async def test_activation_requires_an_adult_profile(app_pool):
    await app_pool.execute("INSERT INTO users (id, age_confirmed_at) VALUES (1, now())")
    assert await repo_users.activate(app_pool, 1) is False  # no profile yet


async def test_status_values_are_constrained(app_pool):
    with pytest.raises(asyncpg.CheckViolationError):
        await app_pool.execute("INSERT INTO users (id, status) VALUES (1, 'admin')")


def test_domain_constants_match_policy():
    assert domain.MIN_AGE == 18
