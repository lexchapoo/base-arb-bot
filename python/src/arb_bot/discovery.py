"""On-chain pool discovery.

Finds Uniswap V3 and Aerodrome pools for a set of tokens by querying the DEX
factories, then verifies each pool's tokens on-chain before building
`PoolMetadata`. No metadata is invented: a pool is only returned if the factory
reports a non-zero address AND the pool contract confirms its own token pair.

Newly discovered pools enrich the route graph (cycles through them are found
when a *watched* pool triggers). Having the Rust ingester also watch a new pool
in real time still requires updating `WATCHED_POOL_ADDRESSES`; discovery does
not silently widen the live subscription.

The core `discover()` takes injected async callables so it is unit-testable
without a live node; `from_settings()` wires the real web3 factory calls.
"""
from __future__ import annotations

from itertools import combinations
from typing import Awaitable, Callable

from .event_graph import PoolMetadata

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
UNIV3_FEE_TIERS = (100, 500, 3000, 10000)

# target, calldata resolver signatures (async):
#   univ3_get_pool(token_a, token_b, fee) -> pool address ("" or zero if none)
#   aero_get_pool(token_a, token_b, stable) -> pool address
#   pool_tokens(pool) -> (token0, token1)
GetPoolFee = Callable[[str, str, int], Awaitable[str]]
GetPoolStable = Callable[[str, str, bool], Awaitable[str]]
PoolTokens = Callable[[str], Awaitable[tuple[str, str]]]


def _is_zero(addr: str | None) -> bool:
    return not addr or int(addr, 16) == 0


class PoolDiscovery:
    def __init__(
        self,
        univ3_get_pool: GetPoolFee | None,
        aero_get_pool: GetPoolStable | None,
        pool_tokens: PoolTokens,
        fee_tiers: tuple[int, ...] = UNIV3_FEE_TIERS,
    ) -> None:
        self._univ3_get_pool = univ3_get_pool
        self._aero_get_pool = aero_get_pool
        self._pool_tokens = pool_tokens
        self._fee_tiers = fee_tiers

    async def _verified(self, pool: str, venue: str, pool_type: str, *, fee: int | None = None, stable: bool | None = None) -> PoolMetadata | None:
        if _is_zero(pool):
            return None
        try:
            token0, token1 = await self._pool_tokens(pool)
        except Exception:
            return None
        if _is_zero(token0) or _is_zero(token1):
            return None
        return PoolMetadata.create(
            address=pool, token0=token0, token1=token1, venue=venue,
            pool_type=pool_type, fee=fee, stable=stable,
        )

    async def discover(self, tokens: list[str]) -> list[PoolMetadata]:
        """Discover verified pools across all unordered token pairs."""
        unique = sorted({t.lower() for t in tokens if t and not _is_zero(t)})
        found: dict[str, PoolMetadata] = {}
        for token_a, token_b in combinations(unique, 2):
            if self._univ3_get_pool is not None:
                for fee in self._fee_tiers:
                    try:
                        pool = await self._univ3_get_pool(token_a, token_b, fee)
                    except Exception:
                        continue
                    meta = await self._verified(pool, "uniswap-v3", "concentrated", fee=fee)
                    if meta is not None:
                        found[meta.address] = meta
            if self._aero_get_pool is not None:
                for stable in (True, False):
                    try:
                        pool = await self._aero_get_pool(token_a, token_b, stable)
                    except Exception:
                        continue
                    meta = await self._verified(pool, "aerodrome", "xyk", stable=stable)
                    if meta is not None:
                        found[meta.address] = meta
        return list(found.values())

    @classmethod
    def from_settings(cls, settings) -> "PoolDiscovery":
        from web3 import AsyncWeb3
        from web3.providers import AsyncHTTPProvider

        w3 = AsyncWeb3(AsyncHTTPProvider(settings.base_http_rpc))
        cs = AsyncWeb3.to_checksum_address

        univ3 = None
        if settings.uniswap_v3_factory:
            factory = w3.eth.contract(
                address=cs(settings.uniswap_v3_factory),
                abi=[{"inputs": [{"type": "address"}, {"type": "address"}, {"type": "uint24"}], "name": "getPool", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"}],
            )
            async def univ3(a: str, b: str, fee: int) -> str:
                return await factory.functions.getPool(cs(a), cs(b), int(fee)).call()

        aero = None
        if settings.aerodrome_factory:
            factory_a = w3.eth.contract(
                address=cs(settings.aerodrome_factory),
                abi=[{"inputs": [{"type": "address"}, {"type": "address"}, {"type": "bool"}], "name": "getPool", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"}],
            )
            async def aero(a: str, b: str, stable: bool) -> str:
                return await factory_a.functions.getPool(cs(a), cs(b), bool(stable)).call()

        pool_abi = [
            {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
        ]

        async def pool_tokens(pool: str) -> tuple[str, str]:
            contract = w3.eth.contract(address=cs(pool), abi=pool_abi)
            return await contract.functions.token0().call(), await contract.functions.token1().call()

        return cls(univ3, aero, pool_tokens)
