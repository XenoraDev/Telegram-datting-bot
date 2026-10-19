"""Business rule: a viewer whose tier is STRICTLY higher than the owner's sees the owner's age /
gender / city even when the owner switched that field off; the same or a lower tier gets exactly the
owner's choice. Tiers are ordered by the existing `plan_badge` level (Free 0 < Premium 1 < Pro 2 <
VIP 3). Only these three profile fields: never a username or any contact.

Every surface is covered: partner card (random chat, reveal and follow notice use it), Friends /
Matches, Likes, Browse (card and advanced filters)."""

import itertools
from datetime import UTC, datetime, timedelta

import pytest

from bot import cards
from bot.db import repo_browse, repo_cards
from tests.factories import give_tier, make_user

VIEWER, OWNER = 1, 2
TIERS = [None, "premium", "pro", "vip"]
LEVEL = {None: 0, "premium": 1, "pro": 2, "vip": 3}
FIELDS = ("age", "gender", "city")
VALUE = {"age": 29, "gender": "f", "city": "Meerut"}
IN_TEXT = {"age": "Age: 29", "gender": "Female", "city": "Meerut"}  # how a shown value is rendered
USERNAME = "owner_private_handle"


def tname(tier):
    return tier or "free"


def outranks(viewer_tier, owner_tier):
    return LEVEL[viewer_tier] > LEVEL[owner_tier]


async def world(pool, viewer_tier, owner_tier, **switches):
    """Viewer 1 and owner 2; `switches` = show_age / show_gender / show_city (default all ON)."""
    await make_user(pool, VIEWER, age=40, gender="m", city="Pune")
    await make_user(pool, OWNER, age=29, gender="f", city="Meerut")
    await pool.execute("UPDATE users SET username = $2 WHERE id = $1", OWNER, USERNAME)
    await pool.execute("UPDATE profiles SET country = 'IN' WHERE user_id = $1", OWNER)
    for field in FIELDS:
        await pool.execute(
            f"UPDATE profiles SET show_{field} = $2 WHERE user_id = $1",  # noqa: S608 - constant
            OWNER,
            switches.get(f"show_{field}", True),
        )
    if viewer_tier:
        await give_tier(pool, VIEWER, viewer_tier)
    if owner_tier:
        await give_tier(pool, OWNER, owner_tier)


ALL_OFF = {"show_age": False, "show_gender": False, "show_city": False}


# ---------------------------------------------------------------- the rule as the business wrote it

# (owner tier, viewer tier, privacy OFF => does the viewer see the field?)
SPEC = [
    ("free", "pro", True),
    ("free", "vip", True),
    ("premium", "pro", True),
    ("premium", "vip", True),
    ("pro", "vip", True),
    ("pro", "pro", False),
    ("vip", "vip", False),
    ("vip", "pro", False),
    ("free", "free", False),
    ("premium", "premium", False),
    ("premium", "free", False),
    # and the strict ordering also covers the remaining pairs:
    ("free", "premium", True),
    ("pro", "premium", False),
    ("pro", "free", False),
    ("vip", "premium", False),
    ("vip", "free", False),
]


@pytest.mark.parametrize(("owner", "viewer", "sees"), SPEC)
@pytest.mark.parametrize("field", FIELDS)
async def test_the_documented_examples_for_each_field(app_pool, svc, owner, viewer, sees, field):
    tier = lambda n: None if n == "free" else n  # noqa: E731
    await world(app_pool, tier(viewer), tier(owner), **{f"show_{field}": False})
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    view = getattr(card, field)
    assert (view.state == "shown") is sees, (owner, viewer, field, view)
    assert view.value == (VALUE[field] if sees else None)
    for other in (f for f in FIELDS if f != field):  # the other two (ON) are not affected
        assert getattr(card, other).state != "hidden"


# ---------------------------------------------------------------- full matrix on the partner card
# (this card is what random chat, the reveal button and the follow notification show)


def expected_state(viewer_tier, owner_tier, owner_shows, field):
    if not (owner_shows or outranks(viewer_tier, owner_tier)):
        return "hidden"
    if field == "city":
        return "shown"
    return "locked" if viewer_tier is None else "shown"


@pytest.mark.parametrize("owner_tier", TIERS, ids=tname)
@pytest.mark.parametrize("viewer_tier", TIERS, ids=tname)
@pytest.mark.parametrize("flags", list(itertools.product([True, False], repeat=3)))
async def test_partner_card_matrix_owner_tier_x_viewer_tier_x_privacy(
    app_pool, svc, owner_tier, viewer_tier, flags
):
    await world(
        app_pool,
        viewer_tier,
        owner_tier,
        show_age=flags[0],
        show_gender=flags[1],
        show_city=flags[2],
    )
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    for field, owner_shows in zip(FIELDS, flags, strict=True):
        view = getattr(card, field)
        assert view.state == expected_state(viewer_tier, owner_tier, owner_shows, field)
        assert view.value == (VALUE[field] if view.state == "shown" else None)
    text = cards.render(card, "en", anonymous=False)
    for field in FIELDS:  # whatever the state, a hidden / locked value is not in the text
        if getattr(card, field).state != "shown":
            assert IN_TEXT[field] not in text
        else:
            assert IN_TEXT[field] in text


async def test_same_tier_and_lower_tier_viewers_keep_the_owners_choice_for_every_pair(
    app_pool, svc
):
    """Preserved behaviour: viewer tier <= owner tier and everything OFF => all three hidden."""
    for owner_tier, viewer_tier in itertools.product(TIERS, TIERS):
        if outranks(viewer_tier, owner_tier):
            continue
        await app_pool.execute("DELETE FROM subscriptions")
        await app_pool.execute("DELETE FROM profiles")
        await app_pool.execute("DELETE FROM users")
        await world(app_pool, viewer_tier, owner_tier, **ALL_OFF)
        card = await svc.cards.for_viewer(VIEWER, OWNER)
        assert [card.age.state, card.gender.state, card.city.state] == ["hidden"] * 3, (
            owner_tier,
            viewer_tier,
        )


# ---------------------------------------------------------------- Friends / Matches


@pytest.mark.parametrize("owner_tier", TIERS, ids=tname)
@pytest.mark.parametrize("viewer_tier", TIERS, ids=tname)
async def test_friends_list_follows_the_same_rule(app_pool, svc, owner_tier, viewer_tier):
    await world(app_pool, viewer_tier, owner_tier, **ALL_OFF)
    await app_pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES ($1, $2)", 1, 2)
    found, total, _hint = await svc.cards.matches(VIEWER)
    assert total == 1
    (card,) = found
    for field in FIELDS:
        assert getattr(card, field).state == expected_state(viewer_tier, owner_tier, False, field)


@pytest.mark.parametrize("owner_tier", TIERS, ids=tname)
@pytest.mark.parametrize("viewer_tier", TIERS, ids=tname)
async def test_the_plain_matches_list_follows_the_same_rule(app_pool, svc, owner_tier, viewer_tier):
    await world(app_pool, viewer_tier, owner_tier, **ALL_OFF)
    await app_pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES ($1, $2)", 1, 2)
    found, total = await svc.browse.matches(VIEWER)
    assert total == 1
    (card,) = found
    seen = outranks(viewer_tier, owner_tier)
    assert (card.age, card.city) == ((29, "Meerut") if seen else (None, None))


# ---------------------------------------------------------------- Likes ("who liked me")


@pytest.mark.parametrize("owner_tier", TIERS, ids=tname)
@pytest.mark.parametrize("viewer_tier", ["pro", "vip"])  # only they may see who liked them
async def test_likes_follow_the_same_rule(app_pool, svc, owner_tier, viewer_tier):
    await world(app_pool, viewer_tier, owner_tier, **ALL_OFF)
    await app_pool.execute(
        "INSERT INTO swipes (swiper_id, target_id, action) VALUES ($1, $2, 'like')", OWNER, VIEWER
    )
    total, found = await svc.cards.who_liked_me(VIEWER)
    assert total == 1 and found is not None
    (card,) = found
    for field in FIELDS:
        assert getattr(card, field).state == expected_state(viewer_tier, owner_tier, False, field)


@pytest.mark.parametrize("viewer_tier", [None, "premium"])
async def test_a_viewer_who_may_not_see_likers_still_gets_no_cards(app_pool, svc, viewer_tier):
    await world(app_pool, viewer_tier, None, **ALL_OFF)  # the owner is Free, the viewer is above
    await app_pool.execute(
        "INSERT INTO swipes (swiper_id, target_id, action) VALUES ($1, $2, 'like')", OWNER, VIEWER
    )
    assert await svc.cards.who_liked_me(VIEWER) == (1, None)  # outranking does not unlock the list


# ---------------------------------------------------------------- Browse (card + filters)


@pytest.mark.parametrize("owner_tier", TIERS, ids=tname)
@pytest.mark.parametrize("viewer_tier", TIERS, ids=tname)
async def test_browse_card_follows_the_same_rule(app_pool, svc, owner_tier, viewer_tier):
    await world(app_pool, viewer_tier, owner_tier, **ALL_OFF)
    card = (await svc.browse.next_card(VIEWER)).card
    seen = outranks(viewer_tier, owner_tier)
    assert (card.age, card.city) == ((29, "Meerut") if seen else (None, None))
    assert not hasattr(card, "gender")  # a Browse card carries no gender at all


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("owner_tier", TIERS, ids=tname)
@pytest.mark.parametrize("viewer_tier", ["pro", "vip"])  # the advanced filters
async def test_browse_filters_match_a_hidden_field_only_for_a_higher_tier_viewer(
    app_pool, svc, viewer_tier, owner_tier, field
):
    await world(app_pool, viewer_tier, owner_tier, **{f"show_{field}": False})
    applied = {
        "age": lambda: svc.filters.set_age(VIEWER, 28, 30),
        "gender": lambda: svc.filters.set_gender(VIEWER, "f"),
        "city": lambda: svc.filters.set_city(VIEWER, "Meerut"),
    }[field]
    assert (await applied()).kind == "ok"
    res = await svc.browse.next_card(VIEWER)
    matched = res.kind == "card"
    assert matched is outranks(viewer_tier, owner_tier)
    if matched and field != "gender":  # the card then shows the field the viewer filtered on
        assert getattr(res.card, field) == VALUE[field]


# ---------------------------------------------------------------- limits of the rule


async def test_the_viewers_entitlement_still_decides_when_the_viewer_outranks(app_pool, svc):
    """Outranking removes the owner's privacy barrier, not the capability check."""
    await world(app_pool, "premium", None, **ALL_OFF)
    await app_pool.execute(
        "UPDATE plan_entitlements SET value = '{\"enabled\": false}' "
        "WHERE feature_key = 'view_partner_age' "
        "AND plan_id = (SELECT id FROM plans WHERE code = 't_premium')"
    )
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    assert card.age.state == "locked" and card.age.value is None  # no capability: masked
    assert (card.gender.state, card.city.state) == ("shown", "shown")


async def test_the_database_only_returns_what_the_rule_allows(app_pool):
    await world(app_pool, None, None, **ALL_OFF)
    one = lambda lvl, a=True, g=True, c=True: repo_cards.one(  # noqa: E731
        app_pool, OWNER, a, g, c, lvl
    )
    same = await one(0)  # same tier: nothing is fetched
    assert (same.age, same.gender, same.city, same.viewer_outranks) == (None, None, None, False)
    higher = await one(1)  # strictly higher: the three fields come back
    assert (higher.age, higher.gender, higher.city, higher.viewer_outranks) == (
        29,
        "f",
        "Meerut",
        True,
    )
    no_cap = await one(3, a=False, g=False, c=False)  # higher but not entitled: still not fetched
    assert (no_cap.age, no_cap.gender, no_cap.city) == (None, None, None)
    assert no_cap.viewer_outranks is True


async def test_no_username_or_contact_is_revealed_to_a_higher_tier_viewer(app_pool, svc):
    await world(app_pool, "vip", None, **ALL_OFF)
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    assert card.age.state == card.gender.state == card.city.state == "shown"
    blob = repr(card) + cards.render(card, "en", anonymous=False)
    assert USERNAME not in blob and "@" not in blob
    assert not any("user" in f or "contact" in f or "phone" in f for f in card.__slots__)
    row = await repo_cards.one(app_pool, OWNER, True, True, True, 3)
    assert not any("user" in f and f != "user_id" for f in row.__slots__)
    # the queries never select a username or a contact column
    source = repo_cards._fields("$2", "$3", "$4", "$5") + repo_browse.OWNER_TIER_JOIN
    assert "username" not in source and "contact" not in source
    found = (await svc.browse.next_card(VIEWER)).card
    assert USERNAME not in repr(found) and found.__slots__ == (
        "public_id",
        "name",
        "age",
        "country",
        "city",
        "bio",
        "followers",
    )


# ---------------------------------------------------------------- the tier of the owner in SQL

NOW = datetime.now(UTC)


async def sql_level(pool, uid):
    return await pool.fetchval(
        f"SELECT {repo_browse.TIER_LEVEL.format(uid='$1')}",  # noqa: S608 - constant fragment
        uid,
    )


async def test_the_sql_tier_level_equals_the_entitlement_services_in_every_situation(app_pool, svc):
    """The owner's tier is computed in SQL (so a hidden value never has to leave the database);
    it must always equal what EntitlementService says plan_badge is."""
    for uid in range(10, 22):
        await make_user(app_pool, uid)
    ent = svc.entitlements

    async def same(uid):
        assert await sql_level(app_pool, uid) == (await ent.get(uid, "plan_badge")).level, uid

    await same(10)  # Free
    for uid, tier in ((11, "premium"), (12, "pro"), (13, "vip")):
        await give_tier(app_pool, uid, tier)
        await same(uid)
    await give_tier(app_pool, 14, "vip")  # expired subscription
    await app_pool.execute(
        "UPDATE subscriptions SET current_period_end = now() - interval '1 day' WHERE user_id = 14"
    )
    await same(14)
    await give_tier(app_pool, 15, "pro")  # canceled subscription
    await app_pool.execute("UPDATE subscriptions SET status = 'canceled' WHERE user_id = 15")
    await same(15)
    await ent.grant(16, "plan_badge", {"enabled": True, "level": 2})  # admin grant
    await same(16)
    gid = await ent.grant(17, "plan_badge", {"enabled": True, "level": 3})
    await ent.revoke_grant(gid)  # revoked grant
    await same(17)
    await ent.grant(18, "plan_badge", {"enabled": True, "level": 3}, expires_at=NOW - timedelta(1))
    await same(18)  # expired grant
    await ent.grant(19, "plan_badge", {"enabled": False, "level": 3})  # disabled value
    await same(19)
    await give_tier(app_pool, 20, "premium")  # two sources: the highest wins
    await ent.grant(20, "plan_badge", {"enabled": True, "level": 3})
    await same(20)
    await give_tier(app_pool, 21, "pro")  # a plan that was switched off
    await app_pool.execute("UPDATE plans SET is_active = false WHERE code = 't_pro'")
    await same(21)
    assert await sql_level(app_pool, 20) == 3 and await sql_level(app_pool, 14) == 0


async def test_a_grant_gives_the_viewer_a_higher_tier_just_like_a_subscription(app_pool, svc):
    await world(app_pool, None, None, **ALL_OFF)
    await svc.entitlements.grant(VIEWER, "plan_badge", {"enabled": True, "level": 2})
    await svc.entitlements.grant(VIEWER, "view_partner_age", {"enabled": True})
    await svc.entitlements.grant(VIEWER, "view_partner_gender", {"enabled": True})
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    assert (card.age.state, card.gender.state, card.city.state) == ("shown",) * 3


async def test_a_viewer_whose_plan_ended_goes_back_to_the_owners_choice(app_pool, svc):
    await world(app_pool, "vip", None, **ALL_OFF)
    assert (await svc.cards.for_viewer(VIEWER, OWNER)).age.state == "shown"
    await app_pool.execute(
        "UPDATE subscriptions SET current_period_end = now() - interval '1 minute' "
        "WHERE user_id = $1",
        VIEWER,
    )
    card = await svc.cards.for_viewer(VIEWER, OWNER)
    assert [card.age.state, card.gender.state, card.city.state] == ["hidden"] * 3


async def test_an_owner_who_upgrades_takes_the_privacy_back_from_a_lower_viewer(app_pool, svc):
    await world(app_pool, "pro", None, **ALL_OFF)
    assert (await svc.cards.for_viewer(VIEWER, OWNER)).city.state == "shown"
    await give_tier(app_pool, OWNER, "pro")  # same tier now
    assert (await svc.cards.for_viewer(VIEWER, OWNER)).city.state == "hidden"
    await give_tier(app_pool, OWNER, "vip")  # above the viewer
    assert (await svc.cards.for_viewer(VIEWER, OWNER)).city.state == "hidden"
