from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from statistics import median
from typing import Iterable


def route_family(route_ids: Iterable[str]) -> str:
    ids = sorted({str(x).lower() for x in route_ids if x})
    return "0x" + sha256("|".join(ids).encode()).hexdigest()


def empirical_survival_bps(durations_ms: Iterable[int], horizon_ms: int, min_samples: int) -> tuple[int | None, int]:
    samples = [int(x) for x in durations_ms if int(x) >= 0]
    if horizon_ms < 0:
        raise ValueError("horizon_ms must be non-negative")
    if len(samples) < min_samples:
        return None, len(samples)
    survived = sum(1 for x in samples if x >= horizon_ms)
    return survived * 10_000 // len(samples), len(samples)


def empirical_capture_bps(outcomes: Iterable[bool], min_samples: int) -> tuple[int | None, int]:
    samples = [bool(x) for x in outcomes]
    if len(samples) < min_samples:
        return None, len(samples)
    return sum(samples) * 10_000 // len(samples), len(samples)


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    action: str
    confidence_bps: int
    survival_probability_bps: int | None
    route_capture_probability_bps: int | None
    features: dict[str, int | str | None]


def choose_shadow_action(*, deterministic_profit_units: int, survival_bps: int | None, capture_bps: int | None,
                         survival_samples: int, capture_samples: int, min_samples: int,
                         now_capture_bps: int, now_survival_bps: int, wait_survival_bps: int) -> ShadowDecision:
    enough = survival_samples >= min_samples and capture_samples >= min_samples and survival_bps is not None and capture_bps is not None
    if not enough or deterministic_profit_units <= 0:
        action = "SKIP"
        confidence = 0 if not enough else min(survival_bps or 0, capture_bps or 0)
    elif capture_bps >= now_capture_bps and survival_bps >= now_survival_bps:
        action = "NOW"
        confidence = min(capture_bps, survival_bps)
    elif survival_bps >= wait_survival_bps:
        action = "WAIT"
        confidence = survival_bps
    else:
        action = "SKIP"
        confidence = 10_000 - survival_bps
    return ShadowDecision(
        action=action,
        confidence_bps=confidence,
        survival_probability_bps=survival_bps,
        route_capture_probability_bps=capture_bps,
        features={
            "deterministic_profit_units": str(deterministic_profit_units),
            "survival_samples": survival_samples,
            "capture_samples": capture_samples,
            "min_samples": min_samples,
            "now_capture_bps": now_capture_bps,
            "now_survival_bps": now_survival_bps,
            "wait_survival_bps": wait_survival_bps,
        },
    )


def competitor_fingerprint(events: Iterable[dict]) -> list[dict]:
    actors: dict[str, dict] = {}
    for event in events:
        actor = str(event.get("source_sender") or "").lower()
        if not actor:
            continue
        pools = {str(x).lower() for x in event.get("tx_touched_pools", []) if x}
        row = actors.setdefault(actor, {"source_sender": actor, "transactions": 0, "multi_pool_transactions": 0, "pools": set(), "timestamps": []})
        row["transactions"] += 1
        if len(pools) >= 2:
            row["multi_pool_transactions"] += 1
        row["pools"].update(pools)
        ts = event.get("observed_at_unix_ms")
        if ts is not None:
            row["timestamps"].append(int(ts))
    out=[]
    for row in actors.values():
        times=sorted(row.pop("timestamps"))
        gaps=[b-a for a,b in zip(times,times[1:]) if b>=a]
        row["unique_pools"] = len(row["pools"])
        row["pools"] = sorted(row["pools"])
        row["median_interarrival_ms"] = int(median(gaps)) if gaps else None
        out.append(row)
    return sorted(out, key=lambda x:(x["multi_pool_transactions"], x["transactions"]), reverse=True)
