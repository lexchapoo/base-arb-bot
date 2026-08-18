"""Minimal in-process Prometheus metrics (no external dependency).

Exposes route-evaluation counters and an evaluation-latency histogram in the
Prometheus text exposition format via `render()`. Thread-safe for the single
event-loop plus worker-thread model FastAPI uses here.
"""
from __future__ import annotations

import threading

_LOCK = threading.Lock()

_counters: dict[str, int] = {
    "arb_route_triggers_total": 0,
    "arb_route_triggers_known_pool_total": 0,
    "arb_route_triggers_unknown_pool_total": 0,
    "arb_route_evaluations_total": 0,
    "arb_route_submission_eligible_total": 0,
}

# Evaluation latency histogram buckets (milliseconds), cumulative on render.
_LATENCY_BUCKETS_MS = [50, 100, 250, 500, 1000, 2000, 5000, 10000, 30000]
_latency_counts = [0] * len(_LATENCY_BUCKETS_MS)
_latency_over = 0  # observations above the largest bucket
_latency_sum = 0.0
_latency_total = 0


def inc(name: str, amount: int = 1) -> None:
    with _LOCK:
        _counters[name] = _counters.get(name, 0) + amount


def observe_latency_ms(value: float) -> None:
    global _latency_over, _latency_sum, _latency_total
    with _LOCK:
        _latency_total += 1
        _latency_sum += value
        for i, bound in enumerate(_LATENCY_BUCKETS_MS):
            if value <= bound:
                _latency_counts[i] += 1
                return
        _latency_over += 1


def render(gauges: dict[str, float] | None = None) -> str:
    lines: list[str] = []
    with _LOCK:
        for name, value in _counters.items():
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")

        lines.append("# TYPE arb_route_eval_latency_ms histogram")
        cumulative = 0
        for i, bound in enumerate(_LATENCY_BUCKETS_MS):
            cumulative += _latency_counts[i]
            lines.append(f'arb_route_eval_latency_ms_bucket{{le="{bound}"}} {cumulative}')
        cumulative += _latency_over
        lines.append(f'arb_route_eval_latency_ms_bucket{{le="+Inf"}} {cumulative}')
        lines.append(f"arb_route_eval_latency_ms_sum {_latency_sum}")
        lines.append(f"arb_route_eval_latency_ms_count {_latency_total}")

    for name, value in (gauges or {}).items():
        lines.append(f"# TYPE {name} gauge")
        lines.append(f"{name} {value}")
    return "\n".join(lines) + "\n"
