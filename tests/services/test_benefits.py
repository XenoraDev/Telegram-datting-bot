"""The benefit lines on /premium come from entitlement data, never from hard-coded copy."""

import json

from bot import tiers
from bot.services import benefits


def rows(tier):
    return {k: json.dumps(v) for k, v in tiers.TIERS[tier].items()}


FREE = {k: json.dumps(v) for k, v in tiers.FREE.items()}


def text(tier, lang="en"):
    return benefits.lines(benefits.effective(FREE, rows(tier)), lang)


def test_free_baseline_shows_limits_and_city_only():
    out = benefits.lines(benefits.effective(FREE), "en")
    assert out[:3] == [
        "💬 100 random chats a day",
        "👀 Browse 100 profiles a day",
        "➕ 50 follows a day",
    ]
    assert out[3] == "🔓 See your partner's city"
    assert len(out) == 4  # no filters / boost / badge on Free


def test_premium_raises_limits_and_unlocks_age_gender():
    out = text("premium")
    assert "💬 200 random chats a day" in out
    assert "🔓 See your partner's age, gender, city" in out
    assert "🏅 Plan badge on your profile" in out and "⚡ Priority in matching" in out
    assert not any("filters" in line or "boost" in line for line in out)


def test_pro_adds_capabilities_and_vip_is_unlimited():
    pro = text("pro")
    assert "🎯 Advanced profile filters" in pro and "💌 See who follows you" in pro
    assert "🚀 Profile boost" in pro and "💬 500 random chats a day" in pro
    vip = text("vip")
    assert "💬 Unlimited random chats" in vip and "➕ Unlimited follows" in vip


def test_a_plan_never_shows_less_than_free():
    # a plan row lower than Free (admin typo) must not promise fewer chats than Free gives
    low = {"random_chat": json.dumps({"enabled": True, "daily_limit": 5})}
    out = benefits.lines(benefits.effective(FREE, low), "en")
    assert out[0] == "💬 100 random chats a day"


def test_a_disabled_or_missing_feature_is_not_listed():
    off = {"random_chat": json.dumps({"enabled": False})}
    assert benefits.lines(benefits.effective(off), "en") == []


def test_hindi_has_every_line():
    out = text("vip", "hi")
    assert out and all(any("ऀ" <= c <= "ॿ" for c in line) for line in out)
