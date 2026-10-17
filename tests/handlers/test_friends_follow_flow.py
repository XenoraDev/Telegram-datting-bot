"""Like -> Follow, Match -> Friends: same logic, clearer words, and a follower count."""

import re

from bot import keyboards as kb
from bot.texts import en, hi
from bot.texts.i18n import tr

A, B, C, D = 7500001, 7500002, 7500003, 7500004
NAMES = {A: "Ann", B: "Bob", C: "Cee", D: "Dee"}


async def handle(pool, uid):
    return (await pool.fetchval("SELECT public_id FROM profiles WHERE user_id = $1", uid)).hex


async def people(tg, *uids):
    for uid in uids:
        await tg.onboard(uid, name=NAMES[uid], city="Rome", bio="hello")


def texts(out, uid=None):
    return [o.text for o in out if o.kind == "SendMessage" and (uid is None or o.chat_id == uid)]


def labels(out):
    return [
        b.text
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


def data(out):
    return [
        b.callback_data
        for o in out
        for row in (getattr(o.markup, "inline_keyboard", None) or [])
        for b in row
    ]


async def follow(tg, pool, who, target):
    return await tg.press(who, f"lk:{await handle(pool, target)}")


# ------------------------------------------------------------------ the words


async def test_the_browse_card_says_follow_and_skip_and_the_data_is_unchanged(tg, app_pool):
    await people(tg, A, B)
    out = await tg.send(A, "/browse")
    assert labels(out)[:2] == ["➕ Follow", "⏭️ Skip"]
    assert [d.split(":")[0] for d in data(out)[:2]] == ["lk", "ps"]  # old messages keep working
    await tg.send(A, "/stop")
    await tg.press(A, "lg:hi")
    out = await tg.send(B, "/browse")
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", B)
    await people(tg, C)
    out = await tg.send(B, "/browse")
    assert labels(out)[:2] == ["➕ फ़ॉलो", "⏭️ आगे बढ़ें"]


async def test_a_mutual_follow_makes_friends_in_both_languages(tg, app_pool):
    await people(tg, A, B)
    await app_pool.execute("UPDATE users SET language = 'hi' WHERE id = $1", B)
    await follow(tg, app_pool, A, B)
    out = await follow(tg, app_pool, B, A)
    assert (
        "You are now friends!" in texts(out, A)[0]
        and "You both follow each other" in texts(out, A)[0]
    )
    assert "अब आप दोस्त हैं!" in texts(out, B)[0]
    assert "Match" not in texts(out, A)[0] and "liked" not in texts(out, A)[0]
    assert labels(out)[:2] == ["👥 My Friends", "🔎 Browse"] or "👥 My Friends" in labels(out)
    assert await app_pool.fetchval("SELECT count(*) FROM matches") == 1  # the same table as before


async def test_the_menu_and_commands_say_friends_and_followers(tg, app_pool):
    await people(tg, A, B)
    out = await tg.send(A, "/help")
    help_text = texts(out, A)[0]
    assert "/friends" in help_text and "/followers" in help_text
    assert "/matches" not in help_text and "/likes" not in help_text
    menu = [b.text for r in kb.main_menu("en").keyboard for b in r]
    assert "👥 My Friends" in menu and not any("Match" in m for m in menu)
    assert "👥 मेरे दोस्त" in [b.text for r in kb.main_menu("hi").keyboard for b in r]


async def test_friends_and_matches_commands_and_the_old_menu_text_all_open_the_list(tg, app_pool):
    await people(tg, A, B)
    await follow(tg, app_pool, A, B)
    await follow(tg, app_pool, B, A)
    for opener in ("/friends", "/matches", "👥 My Friends", "💞 My Matches", "💞 मेरे मैच"):
        out = await tg.send(A, opener)
        assert "Your friends (1)" in texts(out, A)[0].replace("<b>", "").replace("</b>", ""), opener
        assert "Bob" in texts(out, A)[1]


async def test_followers_and_likes_commands_are_the_same_page(tg, app_pool):
    await people(tg, A, B)
    await follow(tg, app_pool, B, A)
    first = texts(await tg.send(A, "/followers"), A)
    second = texts(await tg.send(A, "/likes"), A)
    assert first == second and "follow you" in first[0] and "liked" not in first[0]


async def test_a_legacy_menu_button_still_ends_a_half_finished_conversation(tg, app_pool):
    await people(tg, A)
    await tg.send(A, "/support")
    await tg.press(A, "sp:c:other")
    await tg.send(A, "💞 My Matches")  # an old keyboard: a menu tap, not ticket text
    await tg.send(A, "plain words")
    assert await app_pool.fetchval("SELECT count(*) FROM support_tickets") == 0
    assert kb.LEGACY_MATCHES_LABELS <= kb.MENU_BUTTON_LABELS


def test_no_old_like_or_match_wording_is_left_in_the_texts_users_see():
    keys = (
        "like_btn", "skip_btn", "btn.matches", "matches_btn", "match_new", "matches_none",
        "matches_header", "likes.none", "likes.header", "likes.locked", "limit.like",
        "benefit.likes", "benefit.likes_unlimited", "benefit.who_liked", "myplan.l_likes",
        "match_chat.header", "help",
    )  # fmt: skip
    for key in keys:
        text = en.MESSAGES[key]
        assert not re.search(r"\b(likes?|liked|match(es)?|pass)\b", text, re.I), (key, text)
    for key in keys:
        assert not re.search(r"लाइक|पसंद|मैच", hi.MESSAGES[key]), key
    assert tr("followers.line", "en", n=3) == "👥 Followers: 3"
    assert tr("followers.line", "hi", n=3) == "👥 फ़ॉलोअर्स: 3"


# ------------------------------------------------------------------ the follower count


async def only_bob_is_left_for_ann(tg, pool):
    """Ann has already answered Cee and Dee, so Bob is the one profile she can browse."""
    for other in (C, D):
        await tg.press(A, f"ps:{await handle(pool, other)}")


async def test_the_browse_card_shows_how_many_follow_that_profile(tg, app_pool):
    await people(tg, A, B, C, D)
    await only_bob_is_left_for_ann(tg, app_pool)
    assert "Followers" not in texts(await tg.send(A, "/browse"), A)[0]  # nobody yet: no line
    await app_pool.execute("DELETE FROM swipes WHERE swiper_id = $1 AND target_id = $2", A, B)
    for follower in (C, D):
        await follow(tg, app_pool, follower, B)
    card = texts(await tg.send(A, "/browse"), A)[0]
    assert "Bob" in card and card.rstrip().endswith("👥 Followers: 2")


async def test_only_active_followers_count_and_a_skip_is_not_a_follow(tg, app_pool):
    await people(tg, A, B, C, D)
    await follow(tg, app_pool, C, B)
    await tg.press(D, f"ps:{await handle(app_pool, B)}")  # D skipped Bob
    count = lambda: app_pool.fetchval(  # noqa: E731
        "SELECT count(*) FROM swipes WHERE target_id = $1 AND action = 'like'", B
    )
    assert await count() == 1
    cards = await tg.services.cards.for_viewer(A, B)
    assert cards.followers == 1
    await app_pool.execute("UPDATE users SET status = 'banned' WHERE id = $1", C)
    assert (await tg.services.cards.for_viewer(A, B)).followers == 0


async def test_the_count_is_a_number_and_never_reveals_who(tg, app_pool):
    await people(tg, A, B, C, D)
    await only_bob_is_left_for_ann(tg, app_pool)
    for follower in (C, D):
        await follow(tg, app_pool, follower, B)
    out = await tg.send(A, "/browse")
    shown = " ".join(texts(out, A))
    assert "Followers: 2" in shown
    for who in ("Cee", "Dee", str(C), str(D), f"user{C}", f"user{D}"):
        assert who not in shown
    for b in data(out):
        assert str(C) not in b and str(D) not in b


async def test_friends_cards_show_the_count_but_the_chat_card_does_not(tg, app_pool):
    await people(tg, A, B, C)
    await follow(tg, app_pool, A, B)
    await follow(tg, app_pool, B, A)
    await follow(tg, app_pool, C, B)  # Bob has two followers: Ann and Cee
    friends = texts(await tg.send(A, "/friends"), A)
    assert any("👥 Followers: 2" in t for t in friends)
    pid = await handle(app_pool, B)
    out = await tg.press(A, f"mc:{pid}")  # the anonymous chat card
    assert not any("Followers" in t for t in texts(out))
