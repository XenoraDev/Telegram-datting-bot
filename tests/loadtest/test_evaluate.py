import pytest

from bot.loadtest.evaluate import (
    DEFAULT_THRESHOLDS,
    FAIL,
    PASS,
    WARNING,
    calculate_capacity,
    compare_runs,
    evaluate_run,
    merge_thresholds,
    next_level,
    server_signature,
    validate_thresholds,
)
from bot.loadtest.metrics import (
    HostSampler,
    latency_stats,
    parse_cpu_times,
    parse_meminfo,
    percentile,
)

T = DEFAULT_THRESHOLDS


def summary(**kw):
    base = {
        "ok": 10_000,
        "failed": 0,
        "error_rate_pct": 0.0,
        "p95_ms": 100.0,
        "p99_ms": 200.0,
        "cpu_p95": 40.0,
        "ram_peak": 50.0,
        "swap_peak": 0.0,
        "db_conn_peak": 20.0,
        "db_latency_p95": 5.0,
        "throughput_pct": 100.0,
        "restarts": 0,
    }
    return base | kw


def test_a_healthy_run_passes():
    v = evaluate_run(summary(), T)
    assert v.result == PASS and not v.reasons and v.icon == "🟢"


@pytest.mark.parametrize(
    "change,expected",
    [
        ({"error_rate_pct": 0.6}, WARNING),
        ({"error_rate_pct": 2.5}, FAIL),
        ({"p95_ms": 700.0}, WARNING),
        ({"p95_ms": 2000.0}, FAIL),
        ({"p99_ms": 1200.0}, WARNING),
        ({"cpu_p95": 85.0}, WARNING),
        ({"cpu_p95": 97.0}, FAIL),
        ({"ram_peak": 85.0}, WARNING),
        ({"ram_peak": 93.0}, FAIL),
        ({"swap_peak": 20.0}, WARNING),
        ({"db_conn_peak": 75.0}, WARNING),
        ({"db_conn_peak": 95.0}, FAIL),
        ({"db_latency_p95": 200.0}, WARNING),
        ({"throughput_pct": 95.0}, WARNING),
        ({"throughput_pct": 80.0}, FAIL),
        ({"restarts": 1}, FAIL),
        ({"ok": 0}, FAIL),
        ({"harness_saturated": True}, WARNING),
        ({"failsafe": True, "stop_reason": "memory critical"}, FAIL),
    ],
)
def test_each_measurement_moves_the_verdict(change, expected):
    v = evaluate_run(summary(**change), T)
    assert v.result == expected and v.reasons


def test_the_worst_reason_wins_and_every_reason_is_listed():
    v = evaluate_run(summary(p95_ms=700.0, error_rate_pct=3.0), T)
    assert v.result == FAIL and {r.code for r in v.reasons} == {"p95_ms", "error_rate_pct"}


def test_thresholds_are_configurable():
    strict = merge_thresholds({"p95_warn_ms": 50, "p95_fail_ms": 90})
    assert evaluate_run(summary(), strict).result == FAIL
    assert merge_thresholds({"nonsense": 1}) == T


def test_threshold_form_validation():
    good = {k: str(v) for k, v in T.items()}
    values, errors = validate_thresholds(good)
    assert values == T and not errors
    for key, bad in (
        ("cpu_warn_pct", "-1"),
        ("p95_warn_ms", "x"),
        ("error_fail_pct", "101"),
        ("ram_warn_pct", "nan"),
    ):
        _, errors = validate_thresholds(good | {key: bad})
        assert key in errors, key
    _, errors = validate_thresholds(good | {"cpu_warn_pct": "99", "cpu_fail_pct": "90"})
    assert "cpu_warn_pct" in errors
    _, errors = validate_thresholds(good | {"throughput_warn_pct": "80"})
    assert "throughput_warn_pct" in errors
    _, errors = validate_thresholds({})
    assert errors


# ----------------------------------------------------------------------------- capacity


def run(rid, chats, result, *, status="completed", rate=50, steady=300, **s):
    return {
        "id": rid,
        "status": status,
        "result": result,
        "config": {"chats": chats, "msgs_per_chat_min": rate, "duration_s": steady},
        "summary": summary(**s),
        "server_info": {"cpu_count": 4, "memory_total_mb": 8192},
    }


def test_capacity_follows_the_documented_example():
    runs = [
        run(1, 50, PASS),
        run(2, 100, PASS),
        run(3, 250, PASS, p95_ms=180.0, error_rate_pct=0.02),
        run(4, 500, WARNING, cpu_p95=82.0, p95_ms=620.0),
        run(5, 750, FAIL, error_rate_pct=5.0),
    ]
    cap = calculate_capacity(runs, T)
    assert cap.recommended.chats == 250
    assert cap.warning.chats == 500
    assert cap.failure.chats == 750
    assert (
        "250 chats recommended because the 250-chat test passed with 0.02% errors"
        in cap.explanation
    )
    assert "P95 180 ms" in cap.explanation
    assert "500-chat test is the warning zone" in cap.explanation and "CPU 82%" in cap.explanation
    assert "750-chat test failed" in cap.explanation


def test_a_pass_that_eats_the_safety_margin_is_not_recommended():
    cap = calculate_capacity([run(1, 50, PASS), run(2, 100, PASS, cpu_p95=76.0)], T)
    assert cap.recommended.chats == 50 and cap.warning.chats == 100
    assert "CPU 76.0%" in cap.explanation or "margin" in cap.explanation


def test_a_higher_pass_after_a_warning_is_ignored():
    cap = calculate_capacity([run(1, 50, PASS), run(2, 100, WARNING), run(3, 250, PASS)], T)
    assert cap.recommended.chats == 50 and cap.warning.chats == 100


def test_no_recommendation_when_even_the_lowest_level_failed():
    cap = calculate_capacity([run(1, 50, FAIL)], T)
    assert cap.recommended is None and cap.failure.chats == 50
    assert "No tested level qualifies" in cap.explanation


def test_nothing_tested_states_nothing():
    cap = calculate_capacity([], T)
    assert cap.recommended is None and "no capacity can be stated" in cap.explanation


def test_the_newest_result_at_a_level_supersedes_older_ones():
    cap = calculate_capacity([run(1, 100, FAIL), run(2, 100, PASS)], T)
    assert cap.recommended.chats == 100 and cap.failure is None


def test_incomparable_or_partial_runs_are_not_counted():
    runs = [
        run(1, 50, PASS, status="cancelled"),  # stopped by the admin: partial
        run(2, 100, PASS, rate=10),  # far below the 40-60 msg/min/chat workload
        run(3, 250, PASS, steady=30),  # too short to say anything
        run(4, 500, FAIL, status="aborted"),  # stopped by the fail-safe: counts
    ]
    cap = calculate_capacity(runs, T)
    assert [lv.chats for lv in cap.levels] == [500] and cap.failure.chats == 500


def test_recommended_capacity_is_never_extrapolated_beyond_tested_levels():
    cap = calculate_capacity([run(1, 50, PASS), run(2, 100, PASS)], T)
    assert cap.recommended.chats == 100 and "real limit is still unknown" in cap.explanation


def test_server_signature_and_next_level():
    assert server_signature({"cpu_count": 4, "memory_total_mb": 8192}) == "4 vCPU / 8.0 GB RAM"
    assert server_signature(None) == "? vCPU / ? GB RAM"
    assert next_level(50) == 100 and next_level(250) == 500 and next_level(1000) is None
    assert next_level(60) == 100


def test_comparison_marks_better_worse_and_same():
    before = summary(p95_ms=220.0, cpu_p95=71.0, error_rate_pct=0.0, ram_peak=50.0)
    after = summary(p95_ms=170.0, cpu_p95=54.0, error_rate_pct=1.0, ram_peak=50.5)
    rows = {r["key"]: r for r in compare_runs(before, after)}
    assert rows["p95_ms"]["direction"] == "better" and rows["cpu_p95"]["direction"] == "better"
    assert rows["error_rate_pct"]["direction"] == "worse"
    assert rows["ram_peak"]["direction"] == "same"
    assert compare_runs({}, after)[0]["direction"] == "n/a"
    higher_is_better = {
        r["key"]: r
        for r in compare_runs(summary(throughput_pct=90.0), summary(throughput_pct=99.0))
    }
    assert higher_is_better["throughput_pct"]["direction"] == "better"


# ----------------------------------------------------------------------------- metrics


def test_percentiles_use_nearest_rank():
    data = list(range(1, 101))
    assert percentile(data, 50) == 50 and percentile(data, 95) == 95 and percentile(data, 99) == 99
    assert percentile([], 50) is None and percentile([7], 99) == 7
    stats = latency_stats([5.0, 1.0, 3.0])
    assert stats == {"p50": 3.0, "p95": 5.0, "p99": 5.0, "max": 5.0}
    assert latency_stats([])["max"] is None


def test_proc_parsers():
    a = parse_cpu_times("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 1 1 1")
    assert a == (200.0, 1000.0)
    assert parse_cpu_times("nothing") is None
    mem = parse_meminfo(
        "MemTotal: 8192000 kB\nMemAvailable: 2048000 kB\n"
        "SwapTotal: 1024000 kB\nSwapFree: 1024000 kB\n"
    )
    assert mem["ram_pct"] == 75.0 and mem["swap_pct"] == 0.0 and mem["memory_total_mb"] == 8000
    assert "swap_pct" not in parse_meminfo(
        "MemTotal: 100 kB\nMemAvailable: 50 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB"
    )


def test_host_sampler_reports_cpu_from_two_readings(tmp_path):
    stat, mem = tmp_path / "stat", tmp_path / "mem"
    stat.write_text("cpu  100 0 100 700 100 0 0 0\n")
    mem.write_text("MemTotal: 1000 kB\nMemAvailable: 500 kB\n")
    sampler = HostSampler(str(stat), str(mem))
    stat.write_text("cpu  200 0 200 750 150 0 0 0\n")  # +200 busy of +300 total
    s = sampler.sample()
    assert s.cpu_pct == 66.67 and s.ram_pct == 50.0 and s.swap_pct is None
    assert HostSampler(str(tmp_path / "missing"), str(tmp_path / "gone")).sample().cpu_pct is None
