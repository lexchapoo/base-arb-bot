"""`route_evaluation_budget_seconds` must be a real wall-clock ceiling on one trigger pass.

The setting is documented as "what keeps a trigger from outliving the opportunity it was priced
for", but it was declared and never read: the only bound was a *per-cycle* timeout inside the
optimizer, applied to cycles that all run concurrently. A pass could therefore run far past the
survival window of the opportunity it was pricing.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from arb_bot import api  # noqa: E402
from arb_bot.models import PendingLogTrigger  # noqa: E402


def _trigger() -> PendingLogTrigger:
    return PendingLogTrigger(
        pool_address="0x" + "11" * 20,
        topic0="0x" + "22" * 32,
        transaction_hash="0x" + "33" * 32,
        block_number=50_000_000,
        transaction_index=0,
        log_index=0,
        observed_at_unix_ms=1_700_000_000_000,
        raw_data="0x",
    )


@pytest.mark.asyncio
async def test_pass_exceeding_the_budget_is_abandoned_and_submits_nothing(monkeypatch):
    """An evaluation slower than the budget yields no routes, so nothing can be submitted."""
    monkeypatch.setattr(
        api.pool_graph, "affected_subgraph",
        lambda _addr: {"known_pool": True, "affected_pools": ["0x" + "11" * 20]},
    )

    async def never_finishes(*_a, **_k):
        await asyncio.sleep(30)
        raise AssertionError("evaluation should have been abandoned at the budget")

    monkeypatch.setattr(api.route_optimizer, "evaluate_affected", never_finishes)
    monkeypatch.setattr(api.settings, "route_evaluation_budget_seconds", 0.05)
    # Auto-submit must stay off: the assertion is that nothing reaches the executor.
    monkeypatch.setattr(api.settings, "auto_submit_routes", False)

    body = await api.route_trigger(_trigger())

    assert body["budget_exceeded"] is True
    assert body["reason"] == "route_evaluation_budget_exceeded"
    assert body["route_evaluations"] == []
    assert body["packed_batch_plan"] is None
    assert body["auto_submissions"] == []


@pytest.mark.asyncio
async def test_pass_inside_the_budget_is_not_flagged(monkeypatch):
    """A pass that completes in time must be unaffected by the ceiling."""
    monkeypatch.setattr(
        api.pool_graph, "affected_subgraph",
        lambda _addr: {"known_pool": True, "affected_pools": ["0x" + "11" * 20]},
    )

    async def returns_immediately(*_a, **_k):
        return []

    monkeypatch.setattr(api.route_optimizer, "evaluate_affected", returns_immediately)
    monkeypatch.setattr(api.settings, "route_evaluation_budget_seconds", 5.0)
    monkeypatch.setattr(api.settings, "auto_submit_routes", False)

    body = await api.route_trigger(_trigger())

    assert body["budget_exceeded"] is False
    assert body["reason"] == "route_optimizer_evaluated"
