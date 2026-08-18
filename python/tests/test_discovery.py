import pytest
from arb_bot.discovery import PoolDiscovery

WETH="0x4200000000000000000000000000000000000006"
USDC="0x833589fCd6eDb6E08f4c7C32D4f71b54bdA02913"
ZERO="0x0000000000000000000000000000000000000000"
POOL="0x00000000000000000000000000000000000000A1"


@pytest.mark.asyncio
async def test_discover_verifies_pair_and_records_fee():
    async def univ3(a, b, fee):
        return POOL if fee == 500 else ZERO
    async def aero(a, b, stable):
        return ZERO
    async def tokens(pool):
        return (WETH, USDC)
    found = await PoolDiscovery(univ3, aero, tokens).discover([WETH, USDC])
    assert len(found) == 1
    meta = found[0]
    assert meta.venue == "uniswap-v3"
    assert meta.fee == 500
    assert {meta.token0, meta.token1} == {WETH.lower(), USDC.lower()}


@pytest.mark.asyncio
async def test_discover_skips_zero_and_unverifiable_pools():
    async def univ3(a, b, fee):
        return ZERO
    async def aero(a, b, stable):
        return POOL if stable else ZERO
    async def tokens(pool):
        raise RuntimeError("pool did not expose token0")  # cannot verify
    found = await PoolDiscovery(univ3, aero, tokens).discover([WETH, USDC])
    assert found == []  # a factory returned a pool, but verification failed -> skipped


@pytest.mark.asyncio
async def test_discover_dedups_same_pool_across_queries():
    async def univ3(a, b, fee):
        return POOL  # same pool for every fee tier
    async def tokens(pool):
        return (WETH, USDC)
    found = await PoolDiscovery(univ3, None, tokens).discover([WETH, USDC])
    assert len(found) == 1  # deduped by address
