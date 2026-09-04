#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def env_true(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.lower() == "true"


def percentile(values: list[int], numerator: int, denominator: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = ((len(ordered) - 1) * numerator) // denominator
    return ordered[index]


async def database_report(database_url: str, started_at: datetime) -> dict:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            events = int((await connection.execute(
                text("SELECT count(*) FROM pending_pool_events WHERE created_at >= :started"),
                {"started": started_at},
            )).scalar_one())
            rows = (await connection.execute(text("""
                SELECT route_id, blockers_json, deterministic_net_profit_units,
                       ev_ready, would_submit, evaluation_latency_ms,
                       provider_disagreement, flash_provider, execution_json
                FROM route_evaluations
                WHERE created_at >= :started
                ORDER BY id DESC
            """), {"started": started_at})).mappings().all()
            non_dry = int((await connection.execute(text("""
                SELECT count(*) FROM submissions
                WHERE created_at >= :started AND dry_run = false
            """), {"started": started_at})).scalar_one())
    finally:
        await engine.dispose()

    blockers: Counter[str] = Counter()
    latencies: list[int] = []
    net_profits: list[str] = []
    for row in rows:
        try:
            blockers.update(json.loads(row["blockers_json"] or "[]"))
        except json.JSONDecodeError:
            blockers["invalid_blockers_json"] += 1
        if row["evaluation_latency_ms"] is not None:
            latencies.append(int(row["evaluation_latency_ms"]))
        if row["deterministic_net_profit_units"] is not None:
            net_profits.append(str(row["deterministic_net_profit_units"]))

    return {
        "pending_pool_events": events,
        "candidates": len(rows),
        "rejection_reasons": dict(blockers.most_common()),
        "expected_net_profit_units": net_profits,
        "simulation_successes": sum(bool(row["ev_ready"]) for row in rows),
        "would_submit": sum(bool(row["would_submit"]) for row in rows),
        "evaluation_latency_ms": {
            "samples": len(latencies),
            "min": min(latencies) if latencies else None,
            "median": int(statistics.median(latencies)) if latencies else None,
            "p95": percentile(latencies, 95, 100),
            "max": max(latencies) if latencies else None,
        },
        "provider_disagreement": {
            "measured": sum(row["provider_disagreement"] is not None for row in rows),
            "detected": sum(row["provider_disagreement"] is True for row in rows),
        },
        "flash_providers": flash_provider_report(rows),
        "non_dry_submissions": non_dry,
    }


def flash_provider_report(rows) -> dict:
    """Which venue routes were priced against, and what the other one would have done.

    Two separate things, deliberately kept apart:

      * `priced_against` counts rows per venue. That is just bookkeeping -- it tells you the
        run was configured the way you thought it was.
      * `comparison` is the actual A/B. Each sample is a paired measurement written by
        RouteOptimizer._provider_comparison: the same route, same quotes, same block, priced
        both ways. Unpaired counts across different routes would be near-meaningless here,
        because route-to-route profit variance is orders of magnitude larger than a 5bp fee.

    `would_unblock` is the number that decides whether switching venue is worth anything: the
    routes the baseline's premium made unprofitable that the alternative would have cleared.
    Zero of those over a full run means the fee is not what is standing between this bot and a
    fill, whatever the fee happens to be.
    """
    priced: Counter[str] = Counter()
    samples: list[dict] = []
    for row in rows:
        priced[row["flash_provider"] or "unrecorded"] += 1
        try:
            execution = json.loads(row["execution_json"] or "{}")
        except json.JSONDecodeError:
            continue
        comparison = execution.get("provider_comparison")
        if isinstance(comparison, dict):
            samples.append(comparison)

    deltas = [
        int(sample["net_profit_delta_units"])
        for sample in samples
        if sample.get("net_profit_delta_units") is not None
    ]
    errors: Counter[str] = Counter(
        sample["error"].split(":")[0] for sample in samples if sample.get("error")
    )
    return {
        "priced_against": dict(priced.most_common()),
        "comparison": {
            "enabled": bool(samples),
            "alternative": next((s.get("provider") for s in samples if s.get("provider")), None),
            "paired_samples": len(samples),
            "errored_samples": dict(errors.most_common()),
            "net_profit_delta_units": {
                "samples": len(deltas),
                "min": min(deltas) if deltas else None,
                "median": int(statistics.median(deltas)) if deltas else None,
                "max": max(deltas) if deltas else None,
                # Signed sum: what the whole run would have gained (or lost) on the switch.
                "total": sum(deltas) if deltas else None,
            },
            # The headline: routes the baseline could not submit that the alternative could.
            "would_unblock": sum(bool(s.get("unblocked_by_switch")) for s in samples),
        },
    }


async def observe(duration_seconds: int, poll_seconds: int, smoke: bool) -> int:
    required_seconds = int(os.environ.get("SHADOW_OBSERVATION_SECONDS", "3600"))
    if duration_seconds < required_seconds and not smoke:
        raise ValueError(
            f"duration {duration_seconds}s is below required sustained window {required_seconds}s; "
            "use --smoke only for a non-gating connectivity test"
        )
    if not env_true("DRY_RUN", True) or env_true("LIVE_TRADING"):
        raise RuntimeError("shadow observation requires DRY_RUN=true and LIVE_TRADING=false")

    rust_url = os.environ.get("SHADOW_RUST_HEALTH_URL", "http://localhost:8081/health")
    python_url = os.environ.get("SHADOW_PYTHON_HEALTH_URL", "http://localhost:8080/health")
    database_url = os.environ.get("SHADOW_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    if not database_url:
        raise RuntimeError("SHADOW_DATABASE_URL or DATABASE_URL is required")

    started_at = datetime.now(timezone.utc)
    deadline = time.monotonic() + duration_seconds
    samples: list[dict] = []
    async with httpx.AsyncClient(timeout=4) as client:
        while True:
            sample = {"observed_at": datetime.now(timezone.utc).isoformat()}
            try:
                rust = (await client.get(rust_url)).json()
                sample["rust"] = rust
            except Exception as exc:
                sample["rust_error"] = type(exc).__name__
            try:
                python = (await client.get(python_url)).json()
                sample["python"] = python
            except Exception as exc:
                sample["python_error"] = type(exc).__name__
            samples.append(sample)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_seconds, remaining))

    db = await database_report(database_url, started_at)
    ws_samples = [sample.get("rust", {}).get("chain", {}).get("ws_connected") for sample in samples]
    pending_samples = [sample.get("rust", {}).get("chain", {}).get("pending_logs_enabled") for sample in samples]
    python_dry = [sample.get("python", {}).get("dry_run") for sample in samples if "python" in sample]
    duration_met = duration_seconds >= required_seconds
    gate = {
        "duration_met": duration_met,
        "dry_run_enforced": all(value is True for value in python_dry) and bool(python_dry),
        "live_trading_disabled": not env_true("LIVE_TRADING"),
        "websocket_connected_all_samples": all(value is True for value in ws_samples) and bool(ws_samples),
        "pending_logs_enabled_all_samples": all(value is True for value in pending_samples) and bool(pending_samples),
        "pending_events_observed": db["pending_pool_events"] > 0,
        "route_evaluations_observed": db["candidates"] > 0,
        "exact_simulations_observed": db["simulation_successes"] > 0,
        "provider_disagreement_measured": db["provider_disagreement"]["measured"] > 0,
        "no_live_submissions": db["non_dry_submissions"] == 0,
    }
    # Only gated when the comparison was actually asked for. Turning it on and collecting zero
    # paired samples means the wiring is broken, which is a silent failure the report would
    # otherwise render as a tidy block of nulls.
    if env_true("SHADOW_COMPARE_PROVIDERS"):
        gate["provider_comparison_observed"] = db["flash_providers"]["comparison"]["paired_samples"] > 0
    report = {
        "mode": "shadow-smoke" if smoke else "shadow",
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": duration_seconds,
        "required_duration_seconds": required_seconds,
        "configuration": {"dry_run": True, "live_trading": False},
        "health_samples": len(samples),
        "database": db,
        "gate": gate,
        "passed": all(gate.values()),
    }
    report_dir = Path(os.environ.get("SHADOW_REPORT_DIR", "reports/shadow"))
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    report_path = report_dir / f"shadow-{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"report": str(report_path), "passed": report["passed"], "gate": gate}, indent=2))
    return 0 if report["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe and record a fail-closed Base shadow run")
    parser.add_argument("--duration-seconds", type=int)
    parser.add_argument("--poll-seconds", type=int)
    parser.add_argument("--smoke", action="store_true", help="allow a shorter non-gating observation")
    args = parser.parse_args()
    duration = args.duration_seconds or int(os.environ.get("SHADOW_OBSERVATION_SECONDS", "3600"))
    poll = args.poll_seconds or int(os.environ.get("SHADOW_POLL_INTERVAL_SECONDS", "5"))
    if duration <= 0 or poll <= 0:
        print("duration and poll interval must be positive", file=sys.stderr)
        return 2
    try:
        return asyncio.run(observe(duration, poll, args.smoke))
    except (RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
