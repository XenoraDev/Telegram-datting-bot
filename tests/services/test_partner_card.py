"""Partner card visibility = (the OWNER's privacy switch OR a viewer of a strictly higher tier) AND
the VIEWER's entitlement, decided before anything is built (protected values are never fetched for
a viewer who may not see them). The full owner-tier x viewer-tier matrix is in
test_privacy_tier_outrank.py."""

import itertools
import logging

import pytest

from bot import cards
from bot.db import repo_cards
from tests.factories import give_tier, make_user

VIEWER, OWNER = 1, 2
TIERS = [None, "premium", "pro", "vip"]
LEVEL = {None: 0, "premium": 1, "pro": 2, "vip": 3}


async def setup(pool, tier, *, age=True, gender=True, city=True, owner_tier=None):
    await make_user(pool, VIEWER, age=40, gender="m", city="Pune")
    await make_user(pool, OWNER, age=29, gender="f", city="Meerut")
    await pool.execute("UPDATE profiles SET country = 'IN' WHERE user_id = $1", OWNER)
    await pool.execute(
        "UPDATE profiles SET show_age = $2, show_gender = $3, show_city = $4 WHERE user_id = $1",
        OWNER,
        age,
        gender,
        city,
    )
    if tier:
        await give_tier(pool, VIEWER, tier)
    if owner_tier:
        await give_tier(pool, OWNER, owner_tier)


def expected(tier, owner_shows, field, owner_tier=None):
    if not (owner_shows or LEVEL[tier] > LEVEL[owner_tier]):
        return "hidden"  # the owner's choice stands unless the viewer's tier is strictly higher
    if field == "city":
        return "shown"  # every tier, Free included, may see a city that is not hidden from them
    return "locked" if tier is None else "shown"


@pytest.mark.parametrize("tier", TIERS)
@pytest.mark.parametrize("flags", list(itertools.product([True, False], repeat=3)))
async def test_every_combination_of_owner_privacy_and_viewer_tier(app_pool, svc, tier, flags):
    """A Free owner: any paid viewer is above them, so they see what the owner switched off."""
    await setup(app_pool, tier, age=flags[0], gender=flags[1], city=flags[2])
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    got = (card.age.state, card.gender.state, card.city.state)
    assert got == tuple(
        expected(tier, f, n) for f, n in zip(flags, ("age", "gender", "city"), strict=True)
    )
    # a value exists ONLY in the "shown" state, and then it is the real one
    assert card.age.value == (29 if card.age.state == "shown" else None)
    assert card.gender.value == ("f" if card.gender.state == "shown" else None)
    assert card.city.value == ("Meerut" if card.city.state == "shown" else None)


async def test_privacy_off_hides_a_value_from_a_viewer_of_the_same_tier_even_vip(app_pool, svc):
    await setup(app_pool, "vip", age=False, gender=False, city=False, owner_tier="vip")
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    assert [f.state for f in (card.age, card.gender, card.city)] == ["hidden"] * 3
    text = cards.render(card, "en", anonymous=False)
    assert text.count("🙈 Hidden by the user") == 3
    assert "••" not in text  # hidden is not "locked": a plan would not help
    assert all(v not in text for v in ("29", "Meerut", "Female"))


async def test_privacy_on_but_not_entitled_is_locked_and_the_value_never_leaves_the_database(
    app_pool, svc
):
    await setup(app_pool, None)
    row = await repo_cards.one(app_pool, OWNER, False, False, False)
    assert (row.age, row.gender, row.city) == (None, None, None)  # not even fetched
    assert row.owner_shows_age and row.owner_shows_gender and row.owner_shows_city
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    assert [card.age.state, card.gender.state, card.city.state] == ["locked", "locked", "shown"]


async def test_the_card_has_the_same_fields_for_everyone(app_pool, svc):
    await setup(app_pool, None)
    free = cards.render(await svc.cards.for_viewer(VIEWER, OWNER), "en", anonymous=False)
    await give_tier(app_pool, VIEWER, "vip")
    vip = cards.render(await svc.cards.for_viewer(VIEWER, OWNER), "en", anonymous=False)
    labels = lambda text: [  # noqa: E731
        ln.split(":")[0] for ln in text.splitlines() if ":" in ln and not ln.startswith("🔓")
    ]
    assert (
        labels(free)
        == labels(vip)
        == ["Name", "Age", "Gender", "City", "Country", "Looking for", "About"]
    )
    assert "Age: ••" in free and "Gender: ••" in free and "City: Meerut" in free
    assert "Age: 29" in vip and "Gender: 👩 Female" in vip and "City: Meerut" in vip


@pytest.mark.parametrize("tier", ["premium", "pro", "vip"])
async def test_paid_tiers_see_what_the_owner_allows(app_pool, svc, tier):
    await setup(app_pool, tier)
    text = cards.render(await svc.cards.for_viewer(VIEWER, OWNER), "en", anonymous=False)
    assert (
        "Age: 29" in text
        and "Gender: 👩 Female" in text
        and "🔒" not in text.replace("🔒 Anonymous", "")
    )


async def test_anonymous_chat_card_leaves_out_identity_but_keeps_the_structure(app_pool, svc):
    await setup(app_pool, None)
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    text = cards.render(card, "en", anonymous=True)
    for kept in (
        "Age: ••",
        "Gender: ••",
        "City: Meerut",
        "Country: 🇮🇳 India",
        "Anonymous chat",
    ):
        assert kept in text
    for gone in ("Name:", "Looking for:", "About:", "Name2", "bio"):
        assert gone not in text


async def test_the_partners_badge_comes_from_the_partners_own_entitlements(app_pool, svc):
    await setup(app_pool, None)
    assert "⭐" not in cards.render(
        await svc.cards.for_viewer(VIEWER, OWNER), "en", anonymous=False
    )
    for tier, badge in (("premium", "⭐ Premium"), ("pro", "💎 Pro"), ("vip", "👑 VIP")):
        await app_pool.execute("DELETE FROM subscriptions WHERE user_id = $1", OWNER)
        await give_tier(app_pool, OWNER, tier)
        card = await svc.cards.for_viewer(VIEWER, OWNER)
        assert badge in cards.render(card, "en", anonymous=False)
    await app_pool.execute("DELETE FROM subscriptions WHERE user_id = $1", OWNER)
    await give_tier(app_pool, OWNER, "vip")
    await give_tier(app_pool, VIEWER, "pro")  # the viewer's own tier does not change the badge
    hindi = cards.render(await svc.cards.for_viewer(VIEWER, OWNER), "hi", anonymous=False)
    assert "👑 वीआईपी" in hindi


async def test_the_upgrade_hint_appears_once_not_on_every_card(app_pool, svc):
    await setup(app_pool, None)
    first = await svc.cards.for_viewer(VIEWER, OWNER)
    second = await svc.cards.for_viewer(VIEWER, OWNER)
    assert first.cta is True and second.cta is False
    assert "/premium" in cards.render(first, "en", anonymous=True)
    assert "/premium" not in cards.render(second, "en", anonymous=True)


async def test_a_card_with_nothing_locked_never_nudges(app_pool, svc):
    await setup(app_pool, "premium")
    assert (await svc.cards.for_viewer(VIEWER, OWNER)).cta is False


async def test_the_card_is_html_escaped(app_pool, svc):
    await setup(app_pool, "vip")
    await app_pool.execute(
        "UPDATE profiles SET city = '<i>x</i> & y', bio = '<b>b</b>' WHERE user_id = $1", OWNER
    )
    text = cards.render(await svc.cards.for_viewer(VIEWER, OWNER), "en", anonymous=False)
    assert (
        "&lt;i&gt;x&lt;/i&gt; &amp; y" in text
        and "&lt;b&gt;b&lt;/b&gt;" in text
        and "<i>" not in text
    )


async def test_no_protected_value_reaches_the_logs(app_pool, svc, caplog):
    caplog.set_level(logging.DEBUG)
    await setup(app_pool, None)
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    cards.render(card, "en", anonymous=False)
    cards.render(card, "hi", anonymous=True)
    assert "29" not in caplog.text and "Female" not in caplog.text


async def test_no_card_for_a_user_without_a_profile(app_pool, svc):
    await make_user(app_pool, VIEWER)
    assert await svc.cards.for_viewer(VIEWER, 999) is None


async def test_the_card_service_has_no_side_effect_on_data(app_pool, svc):
    await setup(app_pool, None)
    before = await app_pool.fetch("SELECT * FROM profiles ORDER BY user_id")
    await svc.cards.for_viewer(VIEWER, OWNER)
    assert await app_pool.fetch("SELECT * FROM profiles ORDER BY user_id") == before


# ------------------------------------------------------------------ matches list


async def test_matches_list_applies_the_same_rules_and_one_hint(app_pool, svc):
    await setup(app_pool, None, age=True, gender=False, city=True)
    await make_user(app_pool, 3, age=33, gender="m", city="Goa")
    for other in (OWNER, 3):
        await app_pool.execute(
            "INSERT INTO matches (user_a_id, user_b_id) VALUES ($1, $2)",
            min(VIEWER, other),
            max(VIEWER, other),
        )
    found, total, hint = await svc.cards.matches(VIEWER)
    assert total == 2 and hint is True
    by_name = {c.name: c for c in found}
    assert by_name["Name2"].age.state == "locked" and by_name["Name2"].gender.state == "hidden"
    assert by_name["Name2"].city.value == "Meerut"
    assert by_name["Name3"].age.state == "locked" and by_name["Name3"].gender.state == "locked"
    assert all(c.cta is False for c in found)
    assert (await svc.cards.matches(VIEWER))[2] is False  # the hint is rate limited


async def test_matches_list_hides_blocked_and_ineligible_partners(app_pool, svc):
    await setup(app_pool, "pro")
    await make_user(app_pool, 3)
    await make_user(app_pool, 4, status="banned")
    for other in (OWNER, 3, 4):
        await app_pool.execute(
            "INSERT INTO matches (user_a_id, user_b_id) VALUES ($1, $2)",
            min(VIEWER, other),
            max(VIEWER, other),
        )
    await app_pool.execute("INSERT INTO blocks (blocker_id, blocked_id) VALUES (3, 1)")
    found, total, _ = await svc.cards.matches(VIEWER)
    assert [c.name for c in found] == ["Name2"] and total == 1
    assert found[0].age.value == 29


# ------------------------------------------------------------------ privacy service


async def test_privacy_switches_flip_persist_and_survive_a_profile_edit(app_pool, svc):
    await setup(app_pool, None)
    assert await svc.privacy.get(OWNER) == {"age": True, "gender": True, "city": True}
    assert await svc.privacy.toggle(OWNER, "age") == {"age": False, "gender": True, "city": True}
    assert await svc.privacy.toggle(OWNER, "city") == {"age": False, "gender": True, "city": False}
    assert await svc.privacy.toggle(OWNER, "age") == {"age": True, "gender": True, "city": False}
    from bot.db import repo_profiles

    await repo_profiles.upsert(
        app_pool, OWNER, name="Edited", age=31, gender="f", looking_for="any", city="Pune", bio="b"
    )
    assert await svc.privacy.get(OWNER) == {"age": True, "gender": True, "city": False}


@pytest.mark.parametrize("field", ["name", "bio", "age; DROP TABLE profiles", "", "AGE", None])
async def test_unknown_privacy_fields_are_refused(app_pool, svc, field):
    await setup(app_pool, None)
    assert await svc.privacy.toggle(OWNER, field) is None
    assert await svc.privacy.get(OWNER) == {"age": True, "gender": True, "city": True}


async def test_privacy_for_a_user_without_a_profile(app_pool, svc):
    await make_user(app_pool, 5, with_profile=False)
    assert await svc.privacy.get(5) is None and await svc.privacy.toggle(5, "age") is None
