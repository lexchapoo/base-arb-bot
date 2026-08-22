import asyncio

import pytest

from arb_bot.adapters.slipstream import SlipstreamQuoter
from arb_bot.event_graph import PoolMetadata
from arb_bot.route_engine import AdapterRegistry, RouteOptimizer


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


A = addr(1)
B = addr(2)
P = addr(0x30)


def test_slipstream_venue_is_canonicalised_distinctly_from_aerodrome_v2():
    assert AdapterRegistry.canonical_venue("aerodrome-slipstream") == "aerodrome-slipstream"
    assert AdapterRegistry.canonical_venue("Slipstream") == "aerodrome-slipstream"
    assert AdapterRegistry.canonical_venue("aerodrome") == "aerodrome"


def test_pool_metadata_carries_tick_spacing():
    pool = PoolMetadata.create(
        address=P, token0=A, token1=B,
        venue="aerodrome-slipstream", pool_type="concentrated", tick_spacing=100,
    )
    assert pool.tick_spacing == 100
    assert pool.fee is None


def test_pool_kwargs_pass_tick_spacing_not_fee():
    """Slipstream's pool key is tickSpacing; sending `fee` would quote the wrong pool."""
    pool = PoolMetadata.create(
        address=P, token0=A, token1=B,
        venue="aerodrome-slipstream", pool_type="concentrated", tick_spacing=200,
    )
    kwargs = RouteOptimizer._pool_kwargs(pool)
    assert kwargs["tick_spacing"] == 200
    assert "fee" not in kwargs


def test_slipstream_quote_requires_discovered_tick_spacing():
    quoter = SlipstreamQuoter.__new__(SlipstreamQuoter)
    with pytest.raises(ValueError, match="tick spacing"):
        asyncio.run(quoter.quote_exact_input(A, B, 1000))


def test_slipstream_execution_calldata_encodes_int24_tick_spacing():
    from eth_abi import encode

    from arb_bot.execution import ContractRuntime

    runtime = ContractRuntime.__new__(ContractRuntime)
    runtime.encode = encode
    pool = PoolMetadata.create(
        address=P, token0=A, token1=B,
        venue="aerodrome-slipstream", pool_type="concentrated", tick_spacing=100,
    )
    data = runtime.adapter_data(pool)
    assert data == encode(["int24", "uint160"], [100, 0])

    # A Slipstream pool without tick spacing must fail loudly, never silently fall through
    # to the Aerodrome V2 `stable` encoding.
    missing = PoolMetadata.create(
        address=P, token0=A, token1=B, venue="aerodrome-slipstream", pool_type="concentrated",
    )
    with pytest.raises(RuntimeError, match="missing tick spacing"):
        runtime.adapter_data(missing)


def test_slipstream_adapter_absent_until_quoter_is_configured():
    """AGENTS.md forbids guessed deployments: no address configured means no adapter."""
    registry = AdapterRegistry()
    from arb_bot.config import settings

    if not settings.aerodrome_slipstream_quoter:
        assert registry.get("aerodrome-slipstream") is None
