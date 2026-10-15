from bot.services.rate_limit import MUTE_AFTER_VIOLATIONS, POLICIES, RateLimiter


def test_burst_then_refill(clock):
    rl = RateLimiter(clock)
    cap = int(POLICIES["message"].capacity)
    assert [rl.allow("message", 1) for _ in range(cap)] == [True] * cap
    assert rl.allow("message", 1) is False  # bucket empty
    clock.advance(1 / POLICIES["message"].refill_per_sec + 0.01)  # one token back
    assert rl.allow("message", 1) is True
    assert rl.allow("message", 1) is False


def test_limits_are_per_user_and_per_action(clock):
    rl = RateLimiter(clock)
    for _ in range(int(POLICIES["message"].capacity)):
        rl.allow("message", 1)
    assert rl.allow("message", 1) is False
    assert rl.allow("message", 2) is True  # other user unaffected
    assert rl.allow("callback", 1) is True  # other action unaffected


def test_bucket_never_exceeds_capacity(clock):
    rl = RateLimiter(clock)
    clock.advance(10_000)
    cap = int(POLICIES["chat_start"].capacity)
    assert sum(rl.allow("chat_start", 1) for _ in range(cap + 5)) == cap


def test_chat_start_sustained_rate_is_about_five_per_minute(clock):
    rl = RateLimiter(clock)
    allowed = 0
    for _ in range(120):  # 10 minutes, one attempt every 5s
        allowed += rl.allow("chat_start", 1)
        clock.advance(5)
    assert 50 <= allowed <= 60


def test_violations_expire_after_the_window(clock):
    rl = RateLimiter(clock)
    for _ in range(MUTE_AFTER_VIOLATIONS - 1):
        rl.record_violation(1)
    clock.advance(61)
    assert rl.record_violation(1) == 1


def test_violations_accumulate_and_can_be_cleared(clock):
    rl = RateLimiter(clock)
    assert [rl.record_violation(1) for _ in range(3)] == [1, 2, 3]
    rl.clear_violations(1)
    assert rl.record_violation(1) == 1


def test_prune_forgets_idle_buckets(clock):
    rl = RateLimiter(clock)
    rl.allow("message", 1)
    rl.record_violation(1)
    clock.advance(10_000)
    assert rl.prune() == 1
    assert rl._buckets == {} and rl._violations == {}
