"""Advanced browse filters: entitlement-gated, privacy-safe, server-side."""

import logging

import pytest

from tests.factories import give_tier, make_user

V = 9000  # the viewer


async def browsed(svc, viewer=V, limit=8):
    """Names of everything the viewer is shown, passing each card in turn."""
    seen = []
    for _ in range(limit):
        res = await svc.browse.next_card(viewer)
        if res.kind != "card":
            break
        seen.append(res.card)
        await svc.browse.swipe(viewer, res.card.public_id, "pass")
    return seen


def names(cards):
    return sorted(c.name for c in cards)


async def crowd(pool):
    await make_user(pool, V, age=30, gender="m", city="Pune")
    await make_user(pool, 1, age=22, gender="f", looking="m", city="Pune")
    await make_user(pool, 2, age=27, gender="f", looking="any", city="Delhi")
    await make_user(pool, 3, age=35, gender="m", looking="f", city="pune")
    await make_user(pool, 4, age=27, gender="o", looking="m", city="Mumbai")


# ---------------------------------------------------------------- entitlement


@pytest.mark.parametrize("tier", [None, "premium"])
async def test_free_and_premium_cannot_use_filters(app_pool, svc, tier):
    await crowd(app_pool)
    if tier:
        await give_tier(app_pool, V, tier)
    assert (await svc.filters.state(V)).allowed is False
    for res in (
        await svc.filters.set_age(V, 25, 29),
        await svc.filters.set_gender(V, "f"),
        await svc.filters.set_city(V, "Pune"),
        await svc.filters.set_looking_for(V, "m"),
        await svc.filters.reset(V),
    ):
        assert res.kind == "denied"
    assert await app_pool.fetchval("SELECT count(*) FROM browse_filters") == 0
    assert len(await browsed(svc)) == 4  # plain browsing is unchanged


@pytest.mark.parametrize("tier", ["pro", "vip"])
async def test_pro_and_vip_can_use_filters(app_pool, svc, tier):
    await crowd(app_pool)
    await give_tier(app_pool, V, tier)
    assert (await svc.filters.set_age(V, 25, 29)).kind == "ok"
    assert names(await browsed(svc)) == ["Name2", "Name4"]


async def test_stored_filters_are_ignored_once_the_entitlement_lapses(app_pool, svc):
    await crowd(app_pool)
    sub = await give_tier(app_pool, V, "pro")
    await svc.filters.set_gender(V, "f")
    assert names(await browsed(svc)) == ["Name1", "Name2"]
    # the subscription ends; the stored row stays but nothing is applied any more
    await app_pool.execute("UPDATE subscriptions SET current_period_end = now() WHERE id = $1", sub)
    assert await svc.filters.effective(V) is None
    assert len(await browsed(svc)) == 2  # (cards 1 and 2 were passed above) -> 3 and 4 remain
    assert await app_pool.fetchval("SELECT gender FROM browse_filters WHERE user_id = $1", V) == "f"


async def test_a_plan_with_filters_but_without_age_view_cannot_filter_by_age(app_pool, svc):
    """A filter on a value the viewer may not see would reveal it by elimination."""
    await crowd(app_pool)
    pid = await app_pool.fetchval(
        "INSERT INTO plans (code, name) VALUES ('filt_only', 'x') RETURNING id"
    )
    await app_pool.execute(
        "INSERT INTO plan_entitlements VALUES ($1, 'advanced_filters', '{\"enabled\": true}')", pid
    )
    await app_pool.execute(
        "INSERT INTO subscriptions (user_id, plan_id, current_period_end) "
        "VALUES ($1, $2, now() + interval '5 days')",
        V,
        pid,
    )
    state = await svc.filters.state(V)
    assert state.allowed and state.locked == {"age", "gender"}  # city is free, looking_for is open
    assert (await svc.filters.set_age(V, 25, 29)).kind == "locked_field"
    assert (await svc.filters.set_gender(V, "f")).kind == "locked_field"
    assert (await svc.filters.set_city(V, "Pune")).kind == "ok"
    # even if a row was written some other way, locked fields are not applied
    await app_pool.execute("UPDATE browse_filters SET age_min = 25, age_max = 29, gender = 'f'")
    eff = await svc.filters.effective(V)
    assert (eff.age_min, eff.age_max, eff.gender, eff.city) == (None, None, None, "Pune")


# ---------------------------------------------------------------- the filters themselves


async def test_age_range(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_age(V, 25, 40)
    assert names(await browsed(svc)) == ["Name2", "Name3", "Name4"]


async def test_open_ended_age_ranges(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_age(V, 30, None)
    assert names(await browsed(svc)) == ["Name3"]


async def test_gender(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "vip")
    await svc.filters.set_gender(V, "o")
    assert names(await browsed(svc)) == ["Name4"]


async def test_city_is_case_insensitive(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_city(V, "PUNE")
    assert names(await browsed(svc)) == ["Name1", "Name3"]


async def test_looking_for_includes_people_open_to_anyone(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_looking_for(V, "m")
    assert names(await browsed(svc)) == ["Name1", "Name2", "Name4"]  # m or any
    await svc.filters.reset(V)


async def test_looking_for_any_means_only_open_profiles(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_looking_for(V, "any")
    assert names(await browsed(svc)) == ["Name2"]


async def test_filters_combine_and_reset(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_gender(V, "f")
    await svc.filters.set_city(V, "Pune")
    first = await svc.browse.next_card(V)
    assert first.kind == "card" and first.card.name == "Name1" and first.filtered
    await svc.filters.set_age(V, 40, 50)
    empty = await svc.browse.next_card(V)
    assert empty.kind == "empty" and empty.filtered  # nothing matches: a plain "empty"
    res = await svc.filters.reset(V)
    assert res.kind == "ok" and res.state.filters.is_empty
    assert len(await browsed(svc)) == 4


async def test_an_empty_result_does_not_use_up_a_browse_view(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_age(V, 90, 95)
    assert (await svc.browse.next_card(V)).kind == "empty"
    assert await svc.entitlements.used_today(V, "browse") == 0


# ---------------------------------------------------------------- validation


async def test_invalid_values_are_rejected_and_change_nothing(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    bad = [
        await svc.filters.set_age(V, 30, 20),
        await svc.filters.set_age(V, 12, 20),
        await svc.filters.set_age(V, 20, 100),
        await svc.filters.set_age(V, True, 20),
        await svc.filters.set_gender(V, "x"),
        await svc.filters.set_looking_for(V, "all"),
        await svc.filters.set_city(V, ""),
        await svc.filters.set_city(V, "Pune 123"),
        await svc.filters.set_city(V, "x" * 200),
    ]
    assert [b.kind for b in bad] == ["invalid"] * len(bad)
    assert await app_pool.fetchval("SELECT count(*) FROM browse_filters") == 0


async def test_a_city_that_looks_like_sql_is_just_text(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    res = await svc.filters.set_city(V, "x'; DROP TABLE users; --")
    if res.kind == "ok":  # allowed as plain text: it must simply match nobody
        assert (await svc.browse.next_card(V)).kind == "empty"
    assert await app_pool.fetchval("SELECT count(*) FROM users") == 5


# ---------------------------------------------------------------- privacy


@pytest.mark.parametrize(
    ("switch", "apply", "owner"),
    [
        ("show_age", lambda s: s.filters.set_age(V, 25, 29), 2),
        ("show_gender", lambda s: s.filters.set_gender(V, "f"), 2),
        ("show_city", lambda s: s.filters.set_city(V, "Delhi"), 2),
    ],
)
async def test_a_hidden_value_never_matches_a_filter_on_it(app_pool, svc, switch, apply, owner):
    await crowd(app_pool)
    await give_tier(app_pool, V, "vip")
    await apply(svc)
    await app_pool.execute(f"UPDATE profiles SET {switch} = false WHERE user_id = $1", owner)  # noqa: S608
    await app_pool.execute("DELETE FROM swipes")
    assert "Name2" not in names(await browsed(svc))


async def test_the_card_of_a_filtered_result_hides_what_the_owner_hid(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "vip")
    await app_pool.execute(
        "UPDATE profiles SET show_age = false, show_city = false WHERE user_id = 2"
    )
    await svc.filters.set_gender(V, "f")  # gender is shown: user 2 qualifies
    await svc.filters.set_city(V, None)
    cards = await browsed(svc)
    c2 = next(c for c in cards if c.name == "Name2")
    assert c2.age is None and c2.city is None  # the filter did not unlock anything


async def test_hiding_age_removes_it_from_the_plain_browse_card_too(app_pool, svc):
    await make_user(app_pool, V)
    await make_user(app_pool, 2, age=44, city="Meerut")
    await app_pool.execute(
        "UPDATE profiles SET show_age = false, show_city = false WHERE user_id = 2"
    )
    card = (await svc.browse.next_card(V)).card
    assert card.age is None and card.city is None
    assert "44" not in repr(card) and "Meerut" not in repr(card)


# ---------------------------------------------------------------- safety


async def test_blocked_banned_deleted_and_underage_stay_out_of_filtered_results(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_age(V, 18, 99)
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES ($1, 1)", V)
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (2, $1)", V)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = 3")
    await app_pool.execute("UPDATE users SET status = 'deleted' WHERE id = 4")
    assert await browsed(svc) == []
    await make_user(app_pool, 5, age=26, confirmed=False, status="onboarding")
    assert await browsed(svc) == []


async def test_no_filter_value_reaches_the_logs(app_pool, svc, caplog):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    caplog.set_level(logging.DEBUG)
    await svc.filters.set_city(V, "Delhi")
    await svc.filters.set_age(V, 26, 28)
    await svc.filters.set_gender(V, "f")
    await browsed(svc)
    text = caplog.text
    assert "Delhi" not in text and "26" not in text


async def test_users_filters_are_private_to_them(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await give_tier(app_pool, 1, "pro")
    await svc.filters.set_city(V, "Delhi")
    assert (await svc.filters.state(1)).filters.is_empty
    assert len(await browsed(svc, viewer=1)) == 4


async def test_deleting_the_account_removes_the_filters(app_pool, svc):
    await crowd(app_pool)
    await give_tier(app_pool, V, "pro")
    await svc.filters.set_city(V, "Delhi")
    await svc.account.delete_account(V)
    assert await app_pool.fetchval("SELECT count(*) FROM browse_filters") == 0
