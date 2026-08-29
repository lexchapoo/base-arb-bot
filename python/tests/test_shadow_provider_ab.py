"""The shadow run's A/B between flash-loan venues.

Two halves, tested separately because they fail differently:

  * RouteOptimizer._provider_comparison produces the paired sample, and must never let a
    failure in the measurement disturb the evaluation being measured.
  * scripts_shadow_observe.flash_provider_report aggregates those samples into the number
    that decides whether switching venue is worth anything.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from arb_bot import route_engine
from arb_bot.config import settings
from arb_bot.execution import ExecutionFinalization, FlashProvider
from arb_bot.event_graph import Cycle, PoolMetadata

_OBSERVER = Path(__file__).resolve().parents[2] / "scripts_shadow_observe.py"


def _load_observer():
    spec = importlib.util.spec_from_file_location("shadow_observe", _OBSERVER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:
        pytest.skip(f"shadow observer dependencies unavailable: {exc}")
    return module


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def _finalization(provider: str, net: int | None, eligible: bool, blockers=()) -> ExecutionFinalization:
    return ExecutionFinalization(
        flash_provider=provider, calldata="0x00", route_hash="0x" + "11" * 32,
        target_block=1, deadline=2, simulation_gas_units=1, estimate_gas_units=1,
        gas_price_wei=1, l2_fee_wei=1, l1_fee_upper_bound_wei=1, total_native_fee_wei=1,
        gas_cost_asset_units=1, flashloan_premium_bps=0 if provider == "morpho" else 5,
        flashloan_premium_units=0 if provider == "morpho" else 5,
        available_flash_liquidity=10**24, deterministic_net_profit_units=net,
        simulation_success=True, submission_eligible=eligible, blockers=tuple(blockers),
    )


class _Finalizer:
    def __init__(self, alt: ExecutionFinalization | None = None, raises: Exception | None = None):
        self.alt = alt
        self.raises = raises
        self.calls: list[FlashProvider] = []

    async def finalize(self, cycle, amount_in, amount_out, quotes, observed_block, provider=None):
        self.calls.append(provider)
        if self.raises is not None:
            raise self.raises
        return self.alt


def _cycle():
    p1 = PoolMetadata.create(address=addr(1), token0=addr(10), token1=addr(11),
                             venue="uniswap-v3", pool_type="cl", fee=500)
    p2 = PoolMetadata.create(address=addr(2), token0=addr(10), token1=addr(11),
                             venue="aerodrome", pool_type="xyk", stable=False)
    return Cycle((p1, p2), (addr(10), addr(11), addr(10)))


def _optimizer(finalizer):
    opt = route_engine.RouteOptimizer.__new__(route_engine.RouteOptimizer)
    opt.execution_finalizer = finalizer
    return opt


@pytest.fixture
def shadow(monkeypatch):
    monkeypatch.setattr(settings, "shadow_compare_providers", True)
    monkeypatch.setattr(settings, "dry_run", True)
    monkeypatch.setattr(settings, "live_trading", False)


def _compare(opt, primary):
    return asyncio.run(opt._provider_comparison(_cycle(), 1000, 1200, [], 100, primary))


def test_the_alternative_venue_is_the_one_priced(shadow):
    """An Aave baseline must be compared against Morpho, and vice versa."""
    finalizer = _Finalizer(alt=_finalization("morpho", 170, True))
    result = _compare(_optimizer(finalizer), _finalization("aave", 169, True))
    assert finalizer.calls == [FlashProvider.MORPHO]
    assert result["provider"] == "morpho"
    assert result["baseline_provider"] == "aave"

    finalizer = _Finalizer(alt=_finalization("aave", 169, True))
    _compare(_optimizer(finalizer), _finalization("morpho", 170, True))
    assert finalizer.calls == [FlashProvider.AAVE]


def test_the_delta_is_signed_and_paired(shadow):
    finalizer = _Finalizer(alt=_finalization("morpho", 170, True))
    result = _compare(_optimizer(finalizer), _finalization("aave", 169, True))
    assert result["net_profit_delta_units"] == 1
    assert result["deterministic_net_profit_units"] == 170


def test_a_route_the_premium_blocked_is_flagged(shadow):
    """The headline number: what switching venue would actually have bought."""
    finalizer = _Finalizer(alt=_finalization("morpho", 4, True))
    result = _compare(
        _optimizer(finalizer),
        _finalization("aave", -1, False, ("non_positive_profit_after_flashloan_premium",)),
    )
    assert result["unblocked_by_switch"] is True


def test_a_route_neither_venue_can_submit_is_not_flagged(shadow):
    finalizer = _Finalizer(alt=_finalization("morpho", -5, False))
    result = _compare(_optimizer(finalizer), _finalization("aave", -6, False))
    assert result["unblocked_by_switch"] is False


def test_comparison_is_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "shadow_compare_providers", False)
    finalizer = _Finalizer(alt=_finalization("morpho", 170, True))
    assert _compare(_optimizer(finalizer), _finalization("aave", 169, True)) is None
    assert finalizer.calls == [], "must not spend a second finalize when disabled"


@pytest.mark.parametrize("dry_run,live", [(False, False), (True, True)])
def test_comparison_never_runs_outside_dry_run(monkeypatch, dry_run, live):
    """It doubles the RPC cost of every evaluation for a number nothing acts on."""
    monkeypatch.setattr(settings, "shadow_compare_providers", True)
    monkeypatch.setattr(settings, "dry_run", dry_run)
    monkeypatch.setattr(settings, "live_trading", live)
    finalizer = _Finalizer(alt=_finalization("morpho", 170, True))
    assert _compare(_optimizer(finalizer), _finalization("aave", 169, True)) is None
    assert finalizer.calls == []


def test_a_failed_comparison_is_reported_not_raised(shadow):
    """Instrumentation must never change the outcome it is measuring."""
    finalizer = _Finalizer(raises=TimeoutError("rpc went away"))
    result = _compare(_optimizer(finalizer), _finalization("aave", 169, True))
    assert result["error"].startswith("TimeoutError")
    assert result["provider"] == "morpho"


# --- observer aggregation ------------------------------------------------------------


def _row(provider, comparison=None, **extra):
    execution = {"flash_provider": provider}
    if comparison is not None:
        execution["provider_comparison"] = comparison
    row = {
        "route_id": "0x" + "22" * 32, "blockers_json": "[]",
        "deterministic_net_profit_units": "1", "ev_ready": True, "would_submit": True,
        "evaluation_latency_ms": 5, "provider_disagreement": None,
        "flash_provider": provider, "execution_json": json.dumps(execution),
    }
    row.update(extra)
    return row


def test_rows_are_counted_per_venue():
    module = _load_observer()
    report = module.flash_provider_report([_row("aave"), _row("aave"), _row("morpho")])
    assert report["priced_against"] == {"aave": 2, "morpho": 1}


def test_rows_written_before_the_column_existed_are_not_guessed_at():
    """Backfilling them as 'aave' would let an invented fact into a comparison."""
    module = _load_observer()
    report = module.flash_provider_report([_row(None)])
    assert report["priced_against"] == {"unrecorded": 1}


def test_paired_deltas_are_summarised():
    module = _load_observer()
    rows = [
        _row("aave", {"provider": "morpho", "net_profit_delta_units": 1, "unblocked_by_switch": False}),
        _row("aave", {"provider": "morpho", "net_profit_delta_units": 5, "unblocked_by_switch": True}),
        _row("aave", {"provider": "morpho", "net_profit_delta_units": 3, "unblocked_by_switch": False}),
    ]
    comparison = module.flash_provider_report(rows)["comparison"]
    assert comparison["paired_samples"] == 3
    assert comparison["alternative"] == "morpho"
    assert comparison["net_profit_delta_units"]["median"] == 3
    assert comparison["net_profit_delta_units"]["total"] == 9
    assert comparison["would_unblock"] == 1


def test_errored_samples_are_counted_separately_from_deltas():
    """An RPC failure during the comparison is not a delta of zero."""
    module = _load_observer()
    rows = [
        _row("aave", {"provider": "morpho", "error": "TimeoutError: rpc went away"}),
        _row("aave", {"provider": "morpho", "net_profit_delta_units": 4, "unblocked_by_switch": False}),
    ]
    comparison = module.flash_provider_report(rows)["comparison"]
    assert comparison["paired_samples"] == 2
    assert comparison["net_profit_delta_units"]["samples"] == 1
    assert comparison["errored_samples"] == {"TimeoutError": 1}


def test_no_comparison_rows_reports_disabled_rather_than_zeroes():
    module = _load_observer()
    comparison = module.flash_provider_report([_row("aave")])["comparison"]
    assert comparison["enabled"] is False
    assert comparison["paired_samples"] == 0
    assert comparison["net_profit_delta_units"]["median"] is None


def test_malformed_execution_json_does_not_sink_the_report():
    module = _load_observer()
    rows = [_row("aave", execution_json="{not json"), _row("morpho")]
    report = module.flash_provider_report(rows)
    assert report["priced_against"] == {"aave": 1, "morpho": 1}
