"""PASS / WARNING / FAIL verdicts, the observed-capacity calculation and run comparison.

Pure functions over measured numbers. Nothing here is a claim of scientific truth: the thresholds
are plain, configurable defaults, every verdict lists the measurement that caused it, and the
capacity is the highest level that was actually tested and held, never an extrapolation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .config import LADDER

# key -> (default, min, max). Pairs are (warn, fail); a "margin" is the headroom the RECOMMENDED
# capacity must keep, which is stricter than merely passing.
THRESHOLD_SPEC: dict[str, tuple[float, float, float]] = {
    "error_warn_pct": (0.5, 0.0, 100.0),
    "error_fail_pct": (2.0, 0.0, 100.0),
    "p95_warn_ms": (500.0, 1.0, 600_000.0),
    "p95_fail_ms": (1500.0, 1.0, 600_000.0),
    "p99_warn_ms": (1000.0, 1.0, 600_000.0),
    "p99_fail_ms": (3000.0, 1.0, 600_000.0),
    "throughput_warn_pct": (97.0, 1.0, 100.0),  # achieved / target message rate
    "throughput_fail_pct": (90.0, 1.0, 100.0),
    "cpu_warn_pct": (80.0, 1.0, 100.0),
    "cpu_fail_pct": (95.0, 1.0, 100.0),
    "ram_warn_pct": (80.0, 1.0, 100.0),
    "ram_fail_pct": (92.0, 1.0, 100.0),
    "swap_warn_pct": (10.0, 0.0, 100.0),
    "swap_fail_pct": (50.0, 0.0, 100.0),
    "db_conn_warn_pct": (70.0, 1.0, 100.0),
    "db_conn_fail_pct": (90.0, 1.0, 100.0),
    "db_latency_warn_ms": (100.0, 1.0, 60_000.0),
    "db_latency_fail_ms": (500.0, 1.0, 60_000.0),
    "margin_cpu_pct": (70.0, 1.0, 100.0),
    "margin_ram_pct": (75.0, 1.0, 100.0),
    "margin_p95_ms": (300.0, 1.0, 600_000.0),
    "margin_error_pct": (0.1, 0.0, 100.0),
    # automatic stop (fail-safe)
    "failsafe_ram_pct": (95.0, 50.0, 100.0),
    "failsafe_cpu_pct": (98.0, 50.0, 100.0),
    "failsafe_cpu_seconds": (30.0, 5.0, 600.0),
    "failsafe_db_conn_pct": (95.0, 50.0, 100.0),
    "failsafe_db_error_pct": (25.0, 1.0, 100.0),
    "failsafe_health_fails": (3.0, 1.0, 20.0),
}
DEFAULT_THRESHOLDS: dict[str, float] = {k: v[0] for k, v in THRESHOLD_SPEC.items()}
# (warn, fail) pairs where fail must be on the worse side of warn. Throughput is the reverse.
_PAIRS = [
    ("error_warn_pct", "error_fail_pct"),
    ("p95_warn_ms", "p95_fail_ms"),
    ("p99_warn_ms", "p99_fail_ms"),
    ("cpu_warn_pct", "cpu_fail_pct"),
    ("ram_warn_pct", "ram_fail_pct"),
    ("swap_warn_pct", "swap_fail_pct"),
    ("db_conn_warn_pct", "db_conn_fail_pct"),
    ("db_latency_warn_ms", "db_latency_fail_ms"),
]

PASS, WARNING, FAIL = "PASS", "WARNING", "FAIL"
_RANK = {PASS: 0, WARNING: 1, FAIL: 2}
ICON = {PASS: "🟢", WARNING: "🟡", FAIL: "🔴"}


def merge_thresholds(overrides: Mapping[str, float] | None) -> dict[str, float]:
    """Defaults plus the stored overrides; unknown keys are ignored."""
    out = dict(DEFAULT_THRESHOLDS)
    for key, value in (overrides or {}).items():
        if key in out:
            out[key] = float(value)
    return out


def validate_thresholds(form: Mapping[str, str]) -> tuple[dict[str, float] | None, dict[str, str]]:
    """Parse a full thresholds form. Returns (values, {}) or (None, {key: message})."""
    values: dict[str, float] = {}
    errors: dict[str, str] = {}
    for key, (_default, lo, hi) in THRESHOLD_SPEC.items():
        raw = (form.get(key) or "").strip()
        try:
            value = float(raw)
        except ValueError:
            errors[key] = "Enter a number."
            continue
        if value != value or not lo <= value <= hi:  # NaN or out of range
            errors[key] = f"Must be between {lo:g} and {hi:g}."
            continue
        values[key] = value
    if not errors:
        for warn, fail in _PAIRS:
            if values[warn] > values[fail]:
                errors[warn] = "The warning level cannot be above the fail level."
        if values["throughput_warn_pct"] < values["throughput_fail_pct"]:
            errors["throughput_warn_pct"] = "The warning level cannot be below the fail level."
    return (None, errors) if errors else (values, {})


@dataclass(frozen=True, slots=True)
class Reason:
    level: str  # WARNING | FAIL
    code: str
    text: str

    def as_json(self) -> dict[str, str]:
        return {"level": self.level, "code": self.code, "text": self.text}


@dataclass(frozen=True, slots=True)
class Verdict:
    result: str
    reasons: tuple[Reason, ...]

    @property
    def icon(self) -> str:
        return ICON[self.result]


def _num(summary: Mapping[str, object], key: str) -> float | None:
    value = summary.get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def evaluate_run(summary: Mapping[str, object], thresholds: Mapping[str, float]) -> Verdict:
    """Judge one run from its measured summary. `thresholds` must be a complete mapping."""
    t = merge_thresholds(thresholds)
    reasons: list[Reason] = []

    def higher_is_worse(
        key: str, label: str, warn: str, fail: str, unit: str, fmt: str = ".1f"
    ) -> None:
        value = _num(summary, key)
        if value is None:
            return
        if value >= t[fail]:
            reasons.append(
                Reason(
                    FAIL,
                    key,
                    f"{label} {value:{fmt}}{unit} reached the fail level {t[fail]:g}{unit}",
                )
            )
        elif value >= t[warn]:
            reasons.append(
                Reason(
                    WARNING,
                    key,
                    f"{label} {value:{fmt}}{unit} is above the warning level {t[warn]:g}{unit}",
                )
            )

    if summary.get("stop_reason") and summary.get("failsafe"):
        reasons.append(Reason(FAIL, "failsafe", f"fail-safe stop: {summary['stop_reason']}"))
    if (_num(summary, "restarts") or 0) > 0:
        reasons.append(
            Reason(
                FAIL,
                "restarts",
                f"{int(_num(summary, 'restarts') or 0)} service restart(s) detected in the test",
            )
        )
    if (_num(summary, "ok") or 0) == 0:
        reasons.append(Reason(FAIL, "no_messages", "no message was delivered successfully"))

    higher_is_worse("error_rate_pct", "Error rate", "error_warn_pct", "error_fail_pct", "%", ".2f")
    higher_is_worse("p95_ms", "P95 latency", "p95_warn_ms", "p95_fail_ms", " ms", ".0f")
    higher_is_worse("p99_ms", "P99 latency", "p99_warn_ms", "p99_fail_ms", " ms", ".0f")
    higher_is_worse("cpu_p95", "Sustained CPU (95th pct)", "cpu_warn_pct", "cpu_fail_pct", "%")
    higher_is_worse("ram_peak", "Peak RAM", "ram_warn_pct", "ram_fail_pct", "%")
    higher_is_worse("swap_peak", "Peak swap", "swap_warn_pct", "swap_fail_pct", "%")
    higher_is_worse(
        "db_conn_peak", "DB connection usage", "db_conn_warn_pct", "db_conn_fail_pct", "%"
    )
    higher_is_worse(
        "db_latency_p95",
        "DB latency (P95)",
        "db_latency_warn_ms",
        "db_latency_fail_ms",
        " ms",
        ".0f",
    )

    ratio = _num(summary, "throughput_pct")
    if ratio is not None:
        if ratio < t["throughput_fail_pct"]:
            reasons.append(
                Reason(
                    FAIL,
                    "throughput",
                    f"only {ratio:.1f}% of the target message rate was sustained "
                    f"(fail below {t['throughput_fail_pct']:g}%)",
                )
            )
        elif ratio < t["throughput_warn_pct"]:
            reasons.append(
                Reason(
                    WARNING,
                    "throughput",
                    f"{ratio:.1f}% of the target message rate was sustained "
                    f"(warning below {t['throughput_warn_pct']:g}%)",
                )
            )

    if summary.get("harness_saturated"):
        reasons.append(
            Reason(
                WARNING,
                "harness",
                "the load generator itself was saturated; the server may do more than measured",
            )
        )

    result = max((r.level for r in reasons), key=_RANK.__getitem__, default=PASS)
    return Verdict(result, tuple(reasons))


# ----------------------------------------------------------------------------- capacity


@dataclass(frozen=True, slots=True)
class Level:
    chats: int
    result: str
    run_id: int
    msgs_per_chat_min: int
    p95_ms: float | None
    cpu_p95: float | None
    ram_peak: float | None
    error_rate_pct: float | None
    within_margins: bool
    margin_note: str


@dataclass(frozen=True, slots=True)
class Capacity:
    levels: tuple[Level, ...]
    recommended: Level | None
    warning: Level | None
    failure: Level | None
    explanation: str


def server_signature(info: Mapping[str, object] | None) -> str:
    """Which hardware a run was measured on (so upgrades are compared, not mixed)."""
    info = info or {}
    cpus = info.get("cpu_count")
    mem = info.get("memory_total_mb")
    gb = round(float(mem) / 1024, 1) if isinstance(mem, int | float) else None
    return f"{cpus or '?'} vCPU / {gb if gb is not None else '?'} GB RAM"


def _margins(summary: Mapping[str, object], t: Mapping[str, float]) -> tuple[bool, str]:
    misses = []
    checks = (
        ("cpu_p95", "margin_cpu_pct", "CPU", "%"),
        ("ram_peak", "margin_ram_pct", "RAM", "%"),
        ("p95_ms", "margin_p95_ms", "P95", " ms"),
        ("error_rate_pct", "margin_error_pct", "errors", "%"),
    )
    for key, limit, label, unit in checks:
        value = _num(summary, key)
        if value is not None and value > t[limit]:
            misses.append(f"{label} {value:.1f}{unit} > margin {t[limit]:g}{unit}")
    return (not misses, "; ".join(misses))


def calculate_capacity(
    runs: Sequence[Mapping[str, object]],
    thresholds: Mapping[str, float],
    *,
    min_rate: int = 40,
    max_rate: int = 60,
    min_duration_s: int = 120,
) -> Capacity:
    """Observed capacity from finished runs (newest result per chat level wins).

    Only runs that finished (completed, or stopped by the fail-safe) at a comparable workload
    count: `min_rate`..`max_rate` messages per chat per minute and at least `min_duration_s` of
    steady load. Admin-stopped (partial) runs are shown in the history but never counted.

    Walking the levels upwards, the RECOMMENDED capacity is the highest level reached before the
    first level that is not "PASS with all safety margins kept". The WARNING zone is that first
    non-clean level (when it is a WARNING or a PASS without margin); the OBSERVED FAILURE is the
    lowest FAIL level. A better result at a higher level after a worse one is ignored.
    """
    t = merge_thresholds(thresholds)
    newest: dict[int, Mapping[str, object]] = {}
    for run in sorted(runs, key=lambda r: int(r["id"])):  # type: ignore[call-overload]
        cfg = run.get("config") or {}
        if not isinstance(cfg, Mapping):
            continue
        rate = int(cfg.get("msgs_per_chat_min", 0))  # type: ignore[call-overload]
        steady = int(cfg.get("duration_s", 0))  # type: ignore[call-overload]
        if (
            run.get("status") not in ("completed", "aborted")
            or run.get("result") not in _RANK
            or not min_rate <= rate <= max_rate
            or steady < min_duration_s
        ):
            continue
        newest[int(cfg["chats"])] = run  # type: ignore[call-overload]

    levels: list[Level] = []
    for chats in sorted(newest):
        run = newest[chats]
        summary = run.get("summary") or {}
        assert isinstance(summary, Mapping)  # noqa: S101 - rows written by this module
        ok, note = _margins(summary, t)
        levels.append(
            Level(
                chats=chats,
                result=str(run["result"]),
                run_id=int(run["id"]),  # type: ignore[call-overload]
                msgs_per_chat_min=int(run["config"]["msgs_per_chat_min"]),  # type: ignore[index, call-overload]
                p95_ms=_num(summary, "p95_ms"),
                cpu_p95=_num(summary, "cpu_p95"),
                ram_peak=_num(summary, "ram_peak"),
                error_rate_pct=_num(summary, "error_rate_pct"),
                within_margins=ok,
                margin_note=note,
            )
        )

    recommended: Level | None = None
    warning: Level | None = None
    for level in levels:
        clean = level.result == PASS and level.within_margins
        if clean and warning is None:
            recommended = level
        elif warning is None:
            warning = level if level.result != FAIL else None
            if level.result == FAIL:
                break
    failure = next((lv for lv in levels if lv.result == FAIL), None)
    if warning is not None and failure is not None and warning.chats > failure.chats:
        warning = None
    return Capacity(
        tuple(levels),
        recommended,
        warning,
        failure,
        _explain(levels, recommended, warning, failure, t),
    )


def _describe(level: Level) -> str:
    bits = []
    if level.error_rate_pct is not None:
        bits.append(f"{level.error_rate_pct:.2f}% errors")
    if level.p95_ms is not None:
        bits.append(f"P95 {level.p95_ms:.0f} ms")
    if level.cpu_p95 is not None:
        bits.append(f"CPU {level.cpu_p95:.0f}%")
    if level.ram_peak is not None:
        bits.append(f"RAM {level.ram_peak:.0f}%")
    return ", ".join(bits) or "no numbers recorded"


def _explain(
    levels: Sequence[Level],
    recommended: Level | None,
    warning: Level | None,
    failure: Level | None,
    t: Mapping[str, float],
) -> str:
    if not levels:
        return "No comparable finished test exists yet, so no capacity can be stated."
    parts = []
    if recommended is None:
        first = levels[0]
        why = first.margin_note if first.result == PASS else f"it ended in {first.result}"
        parts.append(
            f"No tested level qualifies: the lowest tested level ({first.chats} chats) did not "
            f"stay inside the safety margins ({why})."
        )
    else:
        parts.append(
            f"{recommended.chats} chats recommended because the {recommended.chats}-chat test "
            f"passed with {_describe(recommended)}, inside the safety margins."
        )
    if warning is not None:
        reason = warning.margin_note if warning.result == PASS else f"it ended in {warning.result}"
        parts.append(
            f"The {warning.chats}-chat test is the warning zone ({_describe(warning)}; {reason})."
        )
    if failure is not None:
        parts.append(f"The {failure.chats}-chat test failed ({_describe(failure)}).")
    if recommended is not None and warning is None and failure is None:
        parts.append("No higher level has been tested, so the real limit is still unknown.")
    return " ".join(parts)


def next_level(chats: int, ladder: Sequence[int] = LADDER) -> int | None:
    """The next step of the manual ladder above `chats`, or None at the top."""
    return next((n for n in ladder if n > chats), None)


# ----------------------------------------------------------------------------- comparison

# metric -> (label, unit, lower_is_better)
COMPARED: tuple[tuple[str, str, str, bool], ...] = (
    ("p95_ms", "P95 latency", " ms", True),
    ("p99_ms", "P99 latency", " ms", True),
    ("cpu_p95", "CPU (95th pct)", "%", True),
    ("ram_peak", "Peak RAM", "%", True),
    ("error_rate_pct", "Error rate", "%", True),
    ("throughput_pct", "Throughput vs target", "%", False),
    ("db_latency_p95", "DB latency (P95)", " ms", True),
    ("db_conn_peak", "DB connections", "%", True),
)


def compare_runs(a: Mapping[str, object], b: Mapping[str, object]) -> list[dict[str, object]]:
    """Row per metric: values of the earlier run `a` and the later `b`, and the direction."""
    rows = []
    for key, label, unit, lower_better in COMPARED:
        before, after = _num(a, key), _num(b, key)
        if before is None or after is None:
            direction = "n/a"
        elif abs(after - before) <= max(abs(before) * 0.02, 0.05):
            direction = "same"
        else:
            better = (after < before) == lower_better
            direction = "better" if better else "worse"
        rows.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "before": before,
                "after": after,
                "direction": direction,
            }
        )
    return rows
