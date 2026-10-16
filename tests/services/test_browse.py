"""P2: browse eligibility, like / pass, mutual matches, matches list, profile reports."""

import asyncio

import pytest

from bot.db import repo_browse, repo_chat, repo_safety
from tests.factories import make_user, start_chat


async def pid(pool, uid: int) -> str:
    return (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", uid)).hex


async def names(svc, uid: int) -> list[str]:
    """Every profile that browsing would show to `uid` (swiping each one away with a pass)."""
    seen = []
    while (card := (await svc.browse.next_card(uid)).card) is not None:
        seen.append(card.name)
        assert (await svc.browse.swipe(uid, card.public_id, "pass")).kind == "passed"
    return seen


async def count(pool, table: str) -> int:
    return await pool.fetchval(f"SELECT count(*) FROM {table}")  # noqa: S608 - test constant


# ---------------- browse eligibility ----------------


async def test_browse_returns_an_eligible_profile_without_identity(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2, age=31, city="Oslo")
    card = (await svc.browse.next_card(1)).card
    assert (card.name, card.age, card.city, card.bio) == ("Name2", 31, "Oslo", "bio")
    assert set(card.__slots__) == {"public_id", "name", "age", "country", "city", "bio"}
    assert "2" != card.public_id and len(card.public_id) == 32


async def test_self_is_never_shown(app_pool, svc):
    await make_user(app_pool, 1)
    assert (await svc.browse.next_card(1)).card is None


@pytest.mark.parametrize(
    "kw",
    [
        {"status": "blocked_underage", "with_profile": False},
        {"status": "banned"},
        {"status": "deleted", "with_profile": False},
        {"status": "onboarding", "confirmed": False, "with_profile": False},
        {"with_profile": False},  # active but no profile
    ],
)
async def test_ineligible_users_are_never_shown(app_pool, svc, kw):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2, **kw)
    assert (await svc.browse.next_card(1)).card is None


async def test_banned_user_with_a_leftover_profile_is_hidden_and_cannot_be_liked(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    handle = await pid(app_pool, 2)
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = 2")
    assert (await svc.browse.next_card(1)).card is None
    assert (await svc.browse.swipe(1, handle, "like")).kind == "unavailable"
    assert await count(app_pool, "swipes") == 0


async def test_blocked_users_are_hidden_in_both_directions(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await make_user(app_pool, 3)
    await repo_safety.add_block(app_pool, 1, 2)  # 1 blocked 2
    await repo_safety.add_block(app_pool, 3, 1)  # 3 blocked 1
    assert (await svc.browse.next_card(1)).card is None
    assert [c for c in await names(svc, 2)] == ["Name3"]  # 2 does not see 1 either
    assert await names(svc, 3) == ["Name2"]  # 3 does not see 1


async def test_swiped_profiles_are_not_shown_again(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await make_user(app_pool, 3)
    first = (await svc.browse.next_card(1)).card
    await svc.browse.swipe(1, first.public_id, "like")
    second = (await svc.browse.next_card(1)).card
    assert second.public_id != first.public_id
    await svc.browse.swipe(1, second.public_id, "pass")
    assert (await svc.browse.next_card(1)).card is None


async def test_looking_for_and_gender_are_not_browse_filters(app_pool, svc):
    await make_user(app_pool, 1, gender="m", looking="f")
    await make_user(app_pool, 2, gender="m", looking="m")
    await make_user(app_pool, 3, gender="o", looking="f")
    assert sorted(await names(svc, 1)) == ["Name2", "Name3"]


async def test_people_who_liked_me_come_first(app_pool, svc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await app_pool.execute("UPDATE users SET last_seen_at = now() WHERE id = 2")  # 2 is fresher
    await svc.browse.swipe(3, await pid(app_pool, 1), "like")
    assert (await svc.browse.next_card(1)).card.name == "Name3"


async def test_user_without_an_adult_profile_cannot_browse(app_pool, svc):
    await make_user(app_pool, 1, status="onboarding", confirmed=False, with_profile=False)
    await make_user(app_pool, 2)
    assert (await svc.browse.next_card(1)).card is None


# ---------------- like / pass ----------------


async def test_like_and_pass_create_one_swipe_row_each(app_pool, svc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    assert (await svc.browse.swipe(1, await pid(app_pool, 2), "like")).kind == "liked"
    assert (await svc.browse.swipe(1, await pid(app_pool, 3), "pass")).kind == "passed"
    rows = await app_pool.fetch(
        "SELECT swiper_id, target_id, action FROM swipes ORDER BY target_id"
    )
    assert [tuple(r) for r in rows] == [(1, 2, "like"), (1, 3, "pass")]


async def test_duplicate_swipe_is_ignored_and_never_changes_the_first_one(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    handle = await pid(app_pool, 2)
    assert (await svc.browse.swipe(1, handle, "pass")).kind == "passed"
    assert (await svc.browse.swipe(1, handle, "pass")).kind == "duplicate"
    assert (await svc.browse.swipe(1, handle, "like")).kind == "duplicate"
    assert await app_pool.fetchval("SELECT action FROM swipes") == "pass"
    assert await count(app_pool, "swipes") == 1


async def test_self_swipe_is_rejected_by_service_and_database(app_pool, svc):
    await make_user(app_pool, 1)
    assert (await svc.browse.swipe(1, await pid(app_pool, 1), "like")).kind == "self"
    assert await count(app_pool, "swipes") == 0
    with pytest.raises(Exception, match="swipes_check|check constraint"):
        await app_pool.execute(
            "INSERT INTO swipes (swiper_id, target_id, action) VALUES (1, 1, 'like')"
        )


@pytest.mark.parametrize("bad", ["", "zz", "x" * 32, "0" * 31, "g" * 32, "0" * 32])
async def test_garbage_or_unknown_handles_do_nothing(app_pool, svc, bad):
    await make_user(app_pool, 1)
    assert (await svc.browse.swipe(1, bad, "like")).kind == "unavailable"
    assert await count(app_pool, "swipes") == 0


async def test_unknown_action_is_rejected(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    assert (await svc.browse.swipe(1, await pid(app_pool, 2), "superlike")).kind == "unavailable"


async def test_ineligible_viewer_cannot_swipe(app_pool, svc):
    await make_user(app_pool, 1, status="banned")
    await make_user(app_pool, 2)
    assert (await svc.browse.swipe(1, await pid(app_pool, 2), "like")).kind == "not_eligible"
    assert await count(app_pool, "swipes") == 0


async def test_blocked_pair_cannot_swipe_each_other(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await repo_safety.add_block(app_pool, 2, 1)
    assert (await svc.browse.swipe(1, await pid(app_pool, 2), "like")).kind == "unavailable"
    assert await count(app_pool, "swipes") == 0


# ---------------- matches ----------------


async def test_one_sided_like_does_not_create_a_match(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    assert (await svc.browse.swipe(1, await pid(app_pool, 2), "like")).kind == "liked"
    assert await count(app_pool, "matches") == 0


async def test_like_then_pass_back_is_no_match(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await svc.browse.swipe(1, await pid(app_pool, 2), "like")
    assert (await svc.browse.swipe(2, await pid(app_pool, 1), "pass")).kind == "passed"
    assert await count(app_pool, "matches") == 0


async def test_mutual_like_creates_exactly_one_canonical_match(app_pool, svc):
    await make_user(app_pool, 5)
    await make_user(app_pool, 2)
    await svc.browse.swipe(5, await pid(app_pool, 2), "like")
    res = await svc.browse.swipe(2, await pid(app_pool, 5), "like")
    assert res.kind == "match" and res.partner_id == 5
    assert res.partner_card.name == "Name5" and res.my_card.name == "Name2"
    rows = await app_pool.fetch("SELECT user_a_id, user_b_id FROM matches")
    assert [tuple(r) for r in rows] == [(2, 5)]  # smaller id first


async def test_repeating_the_mutual_like_does_not_add_a_second_match(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    p1, p2 = await pid(app_pool, 1), await pid(app_pool, 2)
    await svc.browse.swipe(1, p2, "like")
    assert (await svc.browse.swipe(2, p1, "like")).kind == "match"
    assert (await svc.browse.swipe(2, p1, "like")).kind == "duplicate"
    assert (await svc.browse.swipe(1, p2, "like")).kind == "duplicate"
    assert await count(app_pool, "matches") == 1


async def test_database_refuses_a_second_or_reversed_match_row(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    assert await repo_browse.create_match(app_pool, 1, 2)
    assert not await repo_browse.create_match(app_pool, 2, 1)
    with pytest.raises(Exception, match="check"):
        await app_pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES (2, 1)")
    with pytest.raises(Exception, match="unique|duplicate"):
        await app_pool.execute("INSERT INTO matches (user_a_id, user_b_id) VALUES (1, 2)")


@pytest.mark.parametrize("round_", range(8))
async def test_simultaneous_mutual_likes_create_exactly_one_match(app_pool, svc, round_):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    p1, p2 = await pid(app_pool, 1), await pid(app_pool, 2)
    results = await asyncio.gather(svc.browse.swipe(1, p2, "like"), svc.browse.swipe(2, p1, "like"))
    assert sorted(r.kind for r in results) == ["liked", "match"]  # one of them announces it
    assert await count(app_pool, "matches") == 1
    assert await count(app_pool, "swipes") == 2


async def test_many_pairs_liking_at_once_never_lose_or_duplicate_a_match(app_pool, svc):
    users = list(range(10, 22))
    for uid in users:
        await make_user(app_pool, uid)
    handles = {uid: await pid(app_pool, uid) for uid in users}
    pairs = list(zip(users[::2], users[1::2], strict=True))
    jobs = []
    for a, b in pairs:
        jobs += [svc.browse.swipe(a, handles[b], "like"), svc.browse.swipe(b, handles[a], "like")]
        jobs += [svc.browse.swipe(a, handles[b], "like")]  # a duplicate press in the same instant
    await asyncio.gather(*jobs)
    assert await count(app_pool, "matches") == len(pairs)
    assert await count(app_pool, "swipes") == 2 * len(pairs)


async def test_matches_list_shows_only_visible_matches_newest_first(app_pool, svc):
    for uid in (1, 2, 3, 4, 5):
        await make_user(app_pool, uid)
    for other in (2, 3, 4, 5):
        await svc.browse.swipe(1, await pid(app_pool, other), "like")
        await svc.browse.swipe(other, await pid(app_pool, 1), "like")
    cards, total = await svc.browse.matches(1)
    assert total == 4 and [c.name for c in cards] == ["Name5", "Name4", "Name3", "Name2"]
    # a match is mutual: the other side sees user 1
    assert [c.name for c in (await svc.browse.matches(3))[0]] == ["Name1"]
    assert await svc.browse.matches(99) == ([], 0)


async def test_blocked_deleted_banned_or_underage_matches_are_hidden_not_crashing(app_pool, svc):
    for uid in (1, 2, 3, 4, 5):
        await make_user(app_pool, uid)
    for other in (2, 3, 4, 5):
        await svc.browse.swipe(1, await pid(app_pool, other), "like")
        await svc.browse.swipe(other, await pid(app_pool, 1), "like")
    await repo_safety.add_block(app_pool, 1, 2)  # I blocked them
    await repo_safety.add_block(app_pool, 3, 1)  # they blocked me
    await svc.account.delete_account(4)  # deleted
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = 5")
    assert await svc.browse.matches(1) == ([], 0)


async def test_matches_list_is_capped_but_reports_the_total(app_pool, svc):
    await make_user(app_pool, 1)
    for uid in range(2, 16):
        await make_user(app_pool, uid)
        await svc.browse.swipe(1, await pid(app_pool, uid), "like")
        await svc.browse.swipe(uid, await pid(app_pool, 1), "like")
    cards, total = await svc.browse.matches(1)
    assert (len(cards), total) == (10, 14)


async def test_deleting_an_account_removes_its_swipes_and_matches(app_pool, svc):
    for uid in (1, 2, 3):
        await make_user(app_pool, uid)
    await svc.browse.swipe(1, await pid(app_pool, 2), "like")
    await svc.browse.swipe(2, await pid(app_pool, 1), "like")
    await svc.browse.swipe(3, await pid(app_pool, 1), "like")
    await svc.browse.swipe(1, await pid(app_pool, 3), "pass")
    await svc.account.delete_account(1)
    assert await count(app_pool, "swipes") == 0
    assert await count(app_pool, "matches") == 0


# ---------------- report / block from a profile ----------------


async def test_profile_report_uses_the_existing_reports_table_and_blocks(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    res = await svc.moderation.report_profile(1, await pid(app_pool, 2), "scam")
    assert res.kind == "created"
    row = await app_pool.fetchrow("SELECT * FROM reports")
    assert (row["reporter_id"], row["reported_id"], row["source"], row["reason"]) == (
        1,
        2,
        "profile",
        "scam",
    )
    assert row["session_id"] is None and row["context"] is None and row["status"] == "open"
    assert await repo_safety.is_blocked_either_way(app_pool, 1, 2)
    assert (await svc.browse.next_card(1)).card is None  # the reported profile disappears


async def test_profile_report_duplicates_invalid_reasons_and_self_are_refused(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    handle = await pid(app_pool, 2)
    assert (await svc.moderation.report_profile(1, handle, "nonsense")).kind == "invalid_reason"
    assert (
        await svc.moderation.report_profile(1, await pid(app_pool, 1), "spam")
    ).kind == "not_found"
    assert (await svc.moderation.report_profile(1, "zz", "spam")).kind == "not_found"
    assert (await svc.moderation.report_profile(1, handle, "spam")).kind == "created"
    assert (await svc.moderation.report_profile(1, handle, "spam")).kind == "duplicate"
    assert await count(app_pool, "reports") == 1


async def test_profile_reports_share_the_chat_report_rate_limit(app_pool, svc):
    await make_user(app_pool, 1)
    for uid in range(2, 9):
        await make_user(app_pool, uid)
    kinds = [
        (await svc.moderation.report_profile(1, await pid(app_pool, uid), "spam")).kind
        for uid in range(2, 9)
    ]
    assert kinds == ["created"] * 5 + ["rate_limited"] * 2


async def test_three_profile_reporters_trigger_the_same_chat_cooldown(app_pool, svc):
    for uid in (1, 2, 3, 4):
        await make_user(app_pool, uid)
    target = await pid(app_pool, 4)
    results = [
        (await svc.moderation.report_profile(uid, target, "harassment")) for uid in (1, 2, 3)
    ]
    assert [r.restricted for r in results] == [False, False, True]
    assert await repo_safety.active_restriction(app_pool, 4, "chat_cooldown") is not None


async def test_chat_reports_still_work_next_to_profile_reports(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    sid = await start_chat(svc, 1, 2)
    assert (await svc.moderation.report(1, sid, "spam")).kind == "created"
    assert await app_pool.fetchval("SELECT source FROM reports") == "chat"


async def test_profile_block_hides_the_profile_and_ends_a_running_chat(app_pool, svc):
    await make_user(app_pool, 1)
    await make_user(app_pool, 2)
    await start_chat(svc, 1, 2)
    res = await svc.moderation.block_profile(1, await pid(app_pool, 2))
    assert (res.kind, res.partner_id) == ("blocked", 2)
    assert await repo_chat.get_partner(app_pool, 1) is None
    assert (await svc.browse.next_card(1)).card is None and (
        await svc.browse.next_card(2)
    ).card is None


async def test_profile_block_of_unknown_or_own_profile_does_nothing(app_pool, svc):
    await make_user(app_pool, 1)
    assert (await svc.moderation.block_profile(1, await pid(app_pool, 1))).kind == "not_member"
    assert (await svc.moderation.block_profile(1, "0" * 32)).kind == "not_member"
    assert await count(app_pool, "blocks") == 0


async def test_random_chat_is_unaffected_by_swipes(app_pool, svc):
    """A pass / like / match never decides who can random-chat; only blocks do."""
    await make_user(app_pool, 1, gender="m", looking="f")
    await make_user(app_pool, 2, gender="m", looking="f")
    await svc.browse.swipe(1, await pid(app_pool, 2), "pass")
    await svc.browse.swipe(2, await pid(app_pool, 1), "pass")
    assert (await svc.matchmaking.search(1)).kind == "queued"
    assert (await svc.matchmaking.search(2)).kind == "matched"
