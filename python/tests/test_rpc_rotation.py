"""Read calls must spread across endpoints and survive one provider failing.

Quote fan-out is bursty: a single trigger issues a multicall plus a balance query per cycle,
concurrently. Against one endpoint that reliably trips provider rate limits, and a 429 on the
balance query discards the route as `live_balance_query_failed`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")

from arb_bot.multicall import RotatingProviders, endpoint_list  # noqa: E402


def test_endpoint_list_keeps_primary_first_and_dedupes():
    urls = endpoint_list("https://a", "https://b, https://a ,https://c")
    assert urls == ["https://a", "https://b", "https://c"]


def test_endpoint_list_falls_back_to_single_primary():
    assert endpoint_list("https://only", "") == ["https://only"]


def test_rotation_starts_at_a_different_provider_each_call():
    """Without advancing the start position every call lands on the same endpoint, which is the
    single-provider behaviour this change exists to remove."""
    providers = RotatingProviders(["https://a", "https://b", "https://c"])
    firsts = [providers.ordered()[0] for _ in range(6)]
    # three distinct clients, cycling
    assert len({id(c) for c in firsts}) == 3
    assert id(firsts[0]) == id(firsts[3])
    assert id(firsts[0]) != id(firsts[1])


def test_ordered_returns_every_provider_exactly_once():
    """Failover depends on this: the caller must be able to try all endpoints before giving up."""
    providers = RotatingProviders(["https://a", "https://b", "https://c"])
    for _ in range(4):
        batch = providers.ordered()
        assert len(batch) == 3
        assert len({id(c) for c in batch}) == 3


def test_empty_endpoint_list_is_rejected():
    with pytest.raises(ValueError):
        RotatingProviders([])


@pytest.mark.asyncio
async def test_balance_query_fails_over_to_the_next_endpoint(monkeypatch):
    """A rate-limited first endpoint must not discard the route."""
    from arb_bot.route_engine import LiveBalanceProvider

    provider = LiveBalanceProvider(["https://a", "https://b"])
    calls: list[str] = []

    class _Contract:
        def __init__(self, tag): self.tag = tag
        def functions(self): ...

    class _Fn:
        def __init__(self, tag): self.tag = tag
        async def call(self, block_identifier="pending"):
            calls.append(self.tag)
            if self.tag == "a":
                raise RuntimeError("429 Too Many Requests")
            return 12345

    class _Functions:
        def __init__(self, tag): self.tag = tag
        def balanceOf(self, _account): return _Fn(self.tag)

    class _Eth:
        def __init__(self, tag): self.tag = tag
        def contract(self, address=None, abi=None):
            c = type("C", (), {})()
            c.functions = _Functions(self.tag)
            return c

    for w3, tag in zip(provider.providers._clients, ("a", "b")):
        monkeypatch.setattr(w3, "eth", _Eth(tag), raising=False)

    balance = await provider.token_balance("0x" + "11" * 20, "0x" + "22" * 20)
    assert balance == 12345
    assert calls == ["a", "b"], f"expected failover a->b, got {calls}"


@pytest.mark.asyncio
async def test_failover_chain_is_bounded_by_the_per_attempt_timeout():
    """The whole failover chain must fit inside the caller's deadline.

    web3 leaves AsyncHTTPProvider without a request timeout, so a stalled endpoint blocks
    indefinitely. Unbounded, N-endpoint failover blew route_evaluation_budget_seconds and the
    budget guard discarded every route in the pass -- strictly worse than one endpoint failing
    fast. Regression test for that interaction.
    """
    import asyncio
    import time

    providers = RotatingProviders(["https://a", "https://b", "https://c"], attempt_timeout_seconds=0.1)

    async def _hangs(_client):
        await asyncio.sleep(30)

    started = time.monotonic()
    with pytest.raises(Exception):
        await providers.attempt(_hangs)
    elapsed = time.monotonic() - started
    # 3 endpoints x 0.1s, with generous slack for scheduling
    assert elapsed < 1.0, f"failover took {elapsed:.2f}s; must be bounded by attempt timeout"


@pytest.mark.asyncio
async def test_attempt_returns_first_success_without_trying_the_rest():
    providers = RotatingProviders(["https://a", "https://b", "https://c"], attempt_timeout_seconds=1.0)
    seen = []

    async def _ok(client):
        seen.append(id(client))
        return "value"

    assert await providers.attempt(_ok) == "value"
    assert len(seen) == 1, "a successful first attempt must not query further endpoints"
