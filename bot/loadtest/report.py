"""Turn measured windows into a run summary, and a run into exportable CSV / JSON.

Exports contain configuration, thresholds, check verdicts, server facts and aggregated windows.
They never contain a token, password, URL or any message text, because none of those is ever
stored with a run.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from datetime import datetime

from .config import TestConfig
from .metrics import latency_stats, percentile

WINDOW_FIELDS = (
    "seq", "ts", "elapsed_s", "active_chats", "target_msgs_s", "msgs_s", "sent_ok", "sent_failed",
    "db_errors", "p50_ms", "p95_ms", "p99_ms", "max_ms", "cpu_pct", "ram_pct", "swap_pct",
    "worker_cpu_pct", "loop_lag_ms", "in_flight", "db_connections", "db_conn_pct",
    "db_latency_ms", "bot_ok", "web_ok",
)  # fmt: skip
HARNESS_SATURATED_CORE_PCT = 90.0  # the single-threaded load generator is out of CPU


def _col(
    windows: Sequence[Mapping[str, object]], key: str, skip_first: bool = False
) -> list[float]:
    rows = windows[1:] if skip_first and len(windows) > 1 else windows
    return [
        float(w[key]) for w in rows if isinstance(w.get(key), int | float) and w[key] is not None
    ]  # type: ignore[arg-type]


def build_summary(
    windows: Sequence[Mapping[str, object]],
    latencies_ms: Sequence[float],
    totals: Mapping[str, object],
    cfg: TestConfig,
    *,
    restarts: int = 0,
    restart_detail: Sequence[str] = (),
    health_failures: int = 0,
    failsafe: bool = False,
    stop_reason: str | None = None,
    elapsed_s: float = 0.0,
) -> dict[str, object]:
    ok = int(totals.get("ok", 0))  # type: ignore[call-overload]
    failed = int(totals.get("failed", 0))  # type: ignore[call-overload]
    attempted = ok + failed
    expected = sum(
        float(w["target_msgs_s"]) * max(float(w.get("dt", 0) or 0), 0.0)  # type: ignore[arg-type]
        for w in windows
    )
    stats = latency_stats(latencies_ms)
    cpu = _col(windows, "cpu_pct", skip_first=True)
    ram = _col(windows, "ram_pct")
    swap = _col(windows, "swap_pct")
    dbc = _col(windows, "db_conn_pct")
    dbl = sorted(_col(windows, "db_latency_ms"))
    worker = _col(windows, "worker_cpu_pct", skip_first=True)
    lag = _col(windows, "loop_lag_ms")
    rates = _col(windows, "msgs_s")
    return {
        "ok": ok,
        "failed": failed,
        "db_errors": int(totals.get("db_errors", 0)),  # type: ignore[call-overload]
        "shed": int(totals.get("shed", 0)),  # type: ignore[call-overload]
        "rejected": dict(totals.get("rejected", {})),  # type: ignore[call-overload]
        "error_rate_pct": round(failed / attempted * 100, 4) if attempted else 0.0,
        "throughput_pct": round(ok / expected * 100, 2) if expected > 0 else None,
        "msgs_s_avg": round(ok / elapsed_s, 2) if elapsed_s > 0 else None,
        "msgs_s_peak": round(max(rates), 2) if rates else None,
        "target_msgs_s": round(cfg.msgs_per_s, 2),
        "p50_ms": _r(stats["p50"]),
        "p95_ms": _r(stats["p95"]),
        "p99_ms": _r(stats["p99"]),
        "max_ms": _r(stats["max"]),
        "cpu_peak": _r(max(cpu)) if cpu else None,
        "cpu_avg": _r(sum(cpu) / len(cpu)) if cpu else None,
        "cpu_p95": _r(percentile(sorted(cpu), 95)) if cpu else None,
        "ram_peak": _r(max(ram)) if ram else None,
        "swap_peak": _r(max(swap)) if swap else None,
        "db_conn_peak": _r(max(dbc)) if dbc else None,
        "db_latency_p95": _r(percentile(dbl, 95)),
        "db_latency_max": _r(dbl[-1]) if dbl else None,
        "worker_cpu_peak": _r(max(worker)) if worker else None,
        "loop_lag_max_ms": _r(max(lag)) if lag else None,
        "chats_peak": int(max(_col(windows, "active_chats"), default=0)),
        "restarts": restarts,
        "restart_detail": list(restart_detail),
        "health_failures": health_failures,
        "harness_saturated": bool(worker and max(worker) >= HARNESS_SATURATED_CORE_PCT),
        "failsafe": failsafe,
        "stop_reason": stop_reason,
        "duration_actual_s": round(elapsed_s, 1),
        "approximate": False,
    }


def summary_from_windows(
    windows: Sequence[Mapping[str, object]], cfg: TestConfig, *, stop_reason: str, elapsed_s: float
) -> dict[str, object]:
    """Best-effort summary for a run whose worker died. Latency percentiles are the worst window
    (a conservative upper bound), and the summary says so."""
    ok = sum(int(w.get("sent_ok") or 0) for w in windows)  # type: ignore[call-overload]
    failed = sum(int(w.get("sent_failed") or 0) for w in windows)  # type: ignore[call-overload]
    db_errors = sum(int(w.get("db_errors") or 0) for w in windows)  # type: ignore[call-overload]
    shaped = [dict(w) | {"dt": 5.0} for w in windows]
    summary = build_summary(
        shaped,
        [],
        {"ok": ok, "failed": failed, "db_errors": db_errors},
        cfg,
        failsafe=True,
        stop_reason=stop_reason,
        elapsed_s=elapsed_s,
    )
    for key, col in (
        ("p50_ms", "p50_ms"),
        ("p95_ms", "p95_ms"),
        ("p99_ms", "p99_ms"),
        ("max_ms", "max_ms"),
    ):
        values = _col(windows, col)
        summary[key] = _r(max(values)) if values else None
    summary["approximate"] = True
    return summary


def _r(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)


# ----------------------------------------------------------------------------- export


def _plain(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


def window_dict(row: Mapping[str, object]) -> dict[str, object]:
    return {k: _plain(row[k]) for k in WINDOW_FIELDS if k in row}


def to_csv(windows: Sequence[Mapping[str, object]], run_id: int) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["run_id", *WINDOW_FIELDS])
    for w in windows:
        writer.writerow([run_id, *[_cell(_plain(w.get(f))) for f in WINDOW_FIELDS]])
    return out.getvalue()


def _cell(value: object) -> object:
    return "" if value is None else value


def to_json(run: Mapping[str, object], windows: Sequence[Mapping[str, object]]) -> str:
    """The full report. `run` is the parsed run (JSON columns already decoded)."""
    keys = (
        "id", "status", "result", "result_reasons", "partial", "stop_reason", "config",
        "thresholds", "environment", "server_info", "commit_hash", "summary",
        "created_at", "started_at", "finished_at",
    )  # fmt: skip
    body = {k: _plain(run.get(k)) for k in keys}
    return json.dumps(
        {"report": body, "windows": [window_dict(w) for w in windows]}, indent=2, sort_keys=True
    )
