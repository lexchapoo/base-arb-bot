from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Iterable, Protocol

try:
    from web3 import AsyncWeb3
    from web3.providers import AsyncHTTPProvider
except ImportError:  # keeps pure unit tests usable before `make setup` installs runtime deps
    AsyncWeb3 = None  # type: ignore[assignment]
    AsyncHTTPProvider = None  # type: ignore[assignment]

from .event_graph import LivePoolGraph, PoolMetadata


class PoolRegistryStore(Protocol):
    async def load_pools(self) -> list[PoolMetadata]: ...
    async def load_cursor(self, source: str, factory: str, default: int) -> int: ...
    async def save_batch(
        self, *, source: str, factory: str, next_cursor: int,
        pools: Iterable[PoolMetadata], verified_block: int | None = None
    ) -> None: ...

AERODROME_FACTORY_ABI = [
    {"inputs": [], "name": "allPoolsLength", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "i", "type": "uint256"}], "name": "allPools", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
]
AERODROME_POOL_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "stable", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
]
UNISWAP_FACTORY_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "token0", "type": "address"},
        {"indexed": True, "name": "token1", "type": "address"},
        {"indexed": True, "name": "fee", "type": "uint24"},
        {"indexed": False, "name": "tickSpacing", "type": "int24"},
        {"indexed": False, "name": "pool", "type": "address"},
    ],
    "name": "PoolCreated",
    "type": "event",
}]
SLIPSTREAM_FACTORY_ABI = [{
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "token0", "type": "address"},
        {"indexed": True, "name": "token1", "type": "address"},
        {"indexed": True, "name": "tickSpacing", "type": "int24"},
        {"indexed": False, "name": "pool", "type": "address"},
    ],
    "name": "PoolCreated",
    "type": "event",
}]
SLIPSTREAM_POOL_ABI = [
    {"inputs": [], "name": "factory", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "tickSpacing", "outputs": [{"type": "int24"}], "stateMutability": "view", "type": "function"},
]
UNISWAP_POOL_ABI = [
    {"inputs": [], "name": "factory", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token0", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "fee", "outputs": [{"type": "uint24"}], "stateMutability": "view", "type": "function"},
]


def _norm(value: str) -> str:
    return PoolMetadata.normalize_address(value)


def _allow(pool: PoolMetadata, token_allowlist: set[str]) -> bool:
    return not token_allowlist or pool.token0 in token_allowlist or pool.token1 in token_allowlist


@dataclass(slots=True)
class DiscoveryStats:
    aerodrome_seen: int = 0
    uniswap_seen: int = 0
    slipstream_seen: int = 0
    registered: int = 0
    rejected_no_code: int = 0
    rejected_metadata: int = 0

    def as_dict(self) -> dict[str, int]:
        return {k: int(v) for k, v in asdict(self).items()}


class BasePoolDiscovery:
    """Discover only pools anchored to configured, verified Base factories.

    Aerodrome V2 is enumerated through the factory's canonical allPools index.
    Uniswap V3 is reconstructed from the factory's PoolCreated event history and
    each candidate is verified by deployed bytecode plus factory/token/fee reads.
    """

    #: How many consecutive refreshes may retry the same stalled cursor before it is skipped.
    max_cursor_retries: int = 3

    def __init__(
        self,
        rpc_url: str,
        graph: LivePoolGraph,
        *,
        aerodrome_factory: str,
        uniswap_v3_factory: str,
        slipstream_factory: str = "",
        uniswap_from_block: int = 0,
        log_chunk_blocks: int = 20_000,
        token_allowlist: Iterable[str] = (),
        concurrency: int = 16,
        store: PoolRegistryStore | None = None,
        max_cursor_retries: int = 3,
    ) -> None:
        if AsyncWeb3 is None or AsyncHTTPProvider is None:
            raise RuntimeError("web3 is required for automatic pool discovery; run `make setup`")
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        self.graph = graph
        self.aerodrome_factory = _norm(aerodrome_factory) if aerodrome_factory else ""
        self.uniswap_v3_factory = _norm(uniswap_v3_factory) if uniswap_v3_factory else ""
        self.slipstream_factory = _norm(slipstream_factory) if slipstream_factory else ""
        self.uniswap_from_block = max(0, int(uniswap_from_block))
        self.log_chunk_blocks = max(100, int(log_chunk_blocks))
        self.token_allowlist = {_norm(x) for x in token_allowlist if x.strip()}
        self.concurrency = max(1, int(concurrency))
        self._aerodrome_cursor = 0
        self._uniswap_cursor = self.uniswap_from_block
        self._slipstream_cursor = self.uniswap_from_block
        self.store = store
        self._restored = False
        self._lock = asyncio.Lock()
        # A cursor that refuses to advance because the *same* item keeps failing would stall
        # discovery forever (and re-scan the whole tail every refresh). Track how many
        # consecutive passes each cursor position has been retried and give up on it after
        # `max_cursor_retries`, treating the blocker as permanently unresolvable.
        self.max_cursor_retries = max(1, int(max_cursor_retries))
        self._stalls: dict[str, tuple[int, int]] = {}

    def _stall_state(self) -> dict[str, tuple[int, int]]:
        """Per-instance stall counters, created on demand.

        Resolved lazily so instances built without `__init__` still work.
        """
        stalls = self.__dict__.get("_stalls")
        if stalls is None:
            stalls = {}
            self._stalls = stalls
        return stalls

    def _retry_budget_exhausted(self, source: str, cursor: int) -> bool:
        """True once `cursor` has been retried `max_cursor_retries` times without progress."""
        stalls = self._stall_state()
        last_cursor, attempts = stalls.get(source, (None, 0))
        if last_cursor != cursor:
            stalls[source] = (cursor, 1)
            return False
        attempts += 1
        stalls[source] = (cursor, attempts)
        return attempts > self.max_cursor_retries

    def _clear_stall(self, source: str) -> None:
        self._stall_state().pop(source, None)


    async def restore(self) -> dict[str, int]:
        """Rehydrate the graph and cursors before network discovery begins."""
        if self._restored:
            return {
                "restored_pools": 0,
                "aerodrome_cursor": self._aerodrome_cursor,
                "uniswap_cursor": self._uniswap_cursor,
                "slipstream_cursor": self._slipstream_cursor,
            }
        restored = 0
        if self.store is not None:
            for pool in await self.store.load_pools():
                if _allow(pool, self.token_allowlist):
                    self.graph.register(pool)
                    restored += 1
            if self.aerodrome_factory:
                self._aerodrome_cursor = await self.store.load_cursor(
                    "aerodrome-v2", self.aerodrome_factory, 0
                )
            if self.uniswap_v3_factory:
                self._uniswap_cursor = await self.store.load_cursor(
                    "uniswap-v3", self.uniswap_v3_factory, self.uniswap_from_block
                )
            if self.slipstream_factory:
                self._slipstream_cursor = await self.store.load_cursor(
                    "aerodrome-slipstream", self.slipstream_factory, self.uniswap_from_block
                )
        self._restored = True
        return {
            "restored_pools": restored,
            "aerodrome_cursor": self._aerodrome_cursor,
            "uniswap_cursor": self._uniswap_cursor,
            "slipstream_cursor": self._slipstream_cursor,
        }

    async def _has_code(self, address: str) -> bool:
        code = await self.w3.eth.get_code(AsyncWeb3.to_checksum_address(address))
        return bool(code and bytes(code) != b"\x00")

    async def _verify_aerodrome(self, address: str) -> PoolMetadata | None:
        if not await self._has_code(address):
            return None
        c = self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(address), abi=AERODROME_POOL_ABI)
        token0, token1, stable = await asyncio.gather(
            c.functions.token0().call(), c.functions.token1().call(), c.functions.stable().call()
        )
        pool = PoolMetadata.create(
            address=address, token0=token0, token1=token1,
            venue="aerodrome", pool_type="xyk", stable=bool(stable),
        )
        return pool if _allow(pool, self.token_allowlist) else None

    async def discover_aerodrome(self, stats: DiscoveryStats) -> None:
        if not self.aerodrome_factory:
            return
        if not await self._has_code(self.aerodrome_factory):
            raise RuntimeError("configured Aerodrome factory has no deployed code on Base")
        factory = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.aerodrome_factory), abi=AERODROME_FACTORY_ABI
        )
        total = int(await factory.functions.allPoolsLength().call())
        indexes = list(range(self._aerodrome_cursor, total))
        if not indexes:
            return
        sem = asyncio.Semaphore(self.concurrency)
        discovered: list[PoolMetadata] = []
        failed: set[int] = set()

        async def one(i: int) -> None:
            async with sem:
                try:
                    address = str(await factory.functions.allPools(i).call())
                except Exception:
                    # Transient RPC failure: do not let the cursor advance past this index.
                    failed.add(i)
                    return
                stats.aerodrome_seen += 1
                try:
                    pool = await self._verify_aerodrome(address)
                except Exception:
                    stats.rejected_metadata += 1
                    failed.add(i)
                    return
                if pool is not None:
                    discovered.append(pool)

        await asyncio.gather(*(one(i) for i in indexes))
        # Persist the cursor only up to the first index that could not be resolved, so a
        # transient RPC error is retried on the next refresh instead of skipping that pool
        # forever. Verified pools found beyond the gap are still saved (the table is keyed by
        # address, so re-scanning them later is idempotent).
        next_cursor = min(failed) if failed else total
        if failed:
            # A pool that fails deterministically (reverting token0(), self-destructed code,
            # malformed metadata) would otherwise pin the cursor forever and make every
            # refresh re-scan the entire tail. Retry it a bounded number of times, then skip.
            if self._retry_budget_exhausted("aerodrome-v2", next_cursor):
                stats.rejected_metadata += 1
                self._clear_stall("aerodrome-v2")
                next_cursor = min(failed) + 1
        else:
            self._clear_stall("aerodrome-v2")
        if self.store is not None:
            await self.store.save_batch(
                source="aerodrome-v2", factory=self.aerodrome_factory,
                next_cursor=next_cursor, pools=discovered,
            )
        for pool in discovered:
            self.graph.register(pool)
            stats.registered += 1
        self._aerodrome_cursor = next_cursor

    async def _verify_uniswap(self, *, address: str, token0: str, token1: str, fee: int) -> PoolMetadata | None:
        if not await self._has_code(address):
            return None
        c = self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(address), abi=UNISWAP_POOL_ABI)
        actual_factory, actual0, actual1, actual_fee = await asyncio.gather(
            c.functions.factory().call(), c.functions.token0().call(), c.functions.token1().call(), c.functions.fee().call()
        )
        if _norm(actual_factory) != self.uniswap_v3_factory:
            raise ValueError("pool factory mismatch")
        if _norm(actual0) != _norm(token0) or _norm(actual1) != _norm(token1) or int(actual_fee) != int(fee):
            raise ValueError("pool metadata mismatch")
        pool = PoolMetadata.create(
            address=address, token0=token0, token1=token1,
            venue="uniswap-v3", pool_type="concentrated", fee=int(fee),
        )
        return pool if _allow(pool, self.token_allowlist) else None

    async def discover_uniswap(self, stats: DiscoveryStats, latest_block: int) -> None:
        if not self.uniswap_v3_factory:
            return
        if not await self._has_code(self.uniswap_v3_factory):
            raise RuntimeError("configured Uniswap V3 factory has no deployed code on Base")
        factory = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.uniswap_v3_factory), abi=UNISWAP_FACTORY_ABI
        )
        event = factory.events.PoolCreated()
        # hexbytes>=1.0 returns unprefixed hex from .hex(); eth_getLogs requires "0x"-prefixed
        # 32-byte topics, so normalise here rather than relying on the hexbytes version.
        digest = self.w3.keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
        topic0 = digest if digest.startswith("0x") else f"0x{digest}"
        start = self._uniswap_cursor
        while start <= latest_block:
            end = min(start + self.log_chunk_blocks - 1, latest_block)
            logs = await self.w3.eth.get_logs({
                "fromBlock": start,
                "toBlock": end,
                "address": AsyncWeb3.to_checksum_address(self.uniswap_v3_factory),
                "topics": [topic0],
            })
            discovered: list[PoolMetadata] = []
            chunk_failed = False
            for raw in logs:
                stats.uniswap_seen += 1
                try:
                    decoded = event.process_log(raw)
                    args = decoded["args"]
                    pool = await self._verify_uniswap(
                        address=str(args["pool"]), token0=str(args["token0"]),
                        token1=str(args["token1"]), fee=int(args["fee"]),
                    )
                    if pool is not None:
                        discovered.append(pool)
                except ValueError:
                    # Deterministic rejection (factory/token/fee mismatch): re-scanning would
                    # reject it again, so let the cursor move past it.
                    stats.rejected_metadata += 1
                except Exception:
                    # Transient failure (RPC error, timeout): re-scan this chunk next refresh.
                    stats.rejected_metadata += 1
                    chunk_failed = True
            # Hold the cursor on a chunk that had transient failures so nothing is skipped --
            # but only for a bounded number of passes. A log that fails deterministically
            # (a pool whose factory()/token0() reverts, raising something other than
            # ValueError) would otherwise stall discovery at this block range permanently.
            if chunk_failed and self._retry_budget_exhausted("uniswap-v3", start):
                chunk_failed = False
                self._clear_stall("uniswap-v3")
            elif not chunk_failed:
                self._clear_stall("uniswap-v3")
            next_cursor = start if chunk_failed else end + 1
            if self.store is not None:
                await self.store.save_batch(
                    source="uniswap-v3", factory=self.uniswap_v3_factory,
                    next_cursor=next_cursor, pools=discovered, verified_block=end,
                )
            for pool in discovered:
                self.graph.register(pool)
                stats.registered += 1
            self._uniswap_cursor = next_cursor
            if chunk_failed:
                # Stop this pass rather than re-reading the same range in a tight loop; the
                # next scheduled refresh resumes from the un-advanced cursor.
                return
            start = next_cursor

    async def _verify_slipstream(self, *, address: str, token0: str, token1: str, tick_spacing: int) -> PoolMetadata | None:
        if not await self._has_code(address):
            return None
        c = self.w3.eth.contract(address=AsyncWeb3.to_checksum_address(address), abi=SLIPSTREAM_POOL_ABI)
        actual_factory, actual0, actual1, actual_spacing = await asyncio.gather(
            c.functions.factory().call(), c.functions.token0().call(),
            c.functions.token1().call(), c.functions.tickSpacing().call(),
        )
        if _norm(actual_factory) != self.slipstream_factory:
            raise ValueError("pool factory mismatch")
        if _norm(actual0) != _norm(token0) or _norm(actual1) != _norm(token1):
            raise ValueError("pool metadata mismatch")
        if int(actual_spacing) != int(tick_spacing):
            raise ValueError("pool tick spacing mismatch")
        pool = PoolMetadata.create(
            address=address, token0=token0, token1=token1,
            venue="aerodrome-slipstream", pool_type="concentrated",
            tick_spacing=int(tick_spacing),
        )
        return pool if _allow(pool, self.token_allowlist) else None

    async def discover_slipstream(self, stats: DiscoveryStats, latest_block: int) -> None:
        """Reconstruct Slipstream pools from CLFactory PoolCreated history.

        Identical in shape to the Uniswap scan; the pool key is tickSpacing, not fee.
        """
        if not self.slipstream_factory:
            return
        if not await self._has_code(self.slipstream_factory):
            raise RuntimeError("configured Slipstream factory has no deployed code on Base")
        factory = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.slipstream_factory), abi=SLIPSTREAM_FACTORY_ABI
        )
        event = factory.events.PoolCreated()
        digest = self.w3.keccak(text="PoolCreated(address,address,int24,address)").hex()
        topic0 = digest if digest.startswith("0x") else f"0x{digest}"
        start = self._slipstream_cursor
        while start <= latest_block:
            end = min(start + self.log_chunk_blocks - 1, latest_block)
            logs = await self.w3.eth.get_logs({
                "fromBlock": start,
                "toBlock": end,
                "address": AsyncWeb3.to_checksum_address(self.slipstream_factory),
                "topics": [topic0],
            })
            discovered: list[PoolMetadata] = []
            chunk_failed = False
            for raw in logs:
                stats.slipstream_seen += 1
                try:
                    args = event.process_log(raw)["args"]
                    pool = await self._verify_slipstream(
                        address=str(args["pool"]), token0=str(args["token0"]),
                        token1=str(args["token1"]), tick_spacing=int(args["tickSpacing"]),
                    )
                    if pool is not None:
                        discovered.append(pool)
                except ValueError:
                    stats.rejected_metadata += 1
                except Exception:
                    stats.rejected_metadata += 1
                    chunk_failed = True
            if chunk_failed and self._retry_budget_exhausted("aerodrome-slipstream", start):
                chunk_failed = False
                self._clear_stall("aerodrome-slipstream")
            elif not chunk_failed:
                self._clear_stall("aerodrome-slipstream")
            next_cursor = start if chunk_failed else end + 1
            if self.store is not None:
                await self.store.save_batch(
                    source="aerodrome-slipstream", factory=self.slipstream_factory,
                    next_cursor=next_cursor, pools=discovered, verified_block=end,
                )
            for pool in discovered:
                self.graph.register(pool)
                stats.registered += 1
            self._slipstream_cursor = next_cursor
            if chunk_failed:
                return
            start = next_cursor

    async def refresh(self) -> dict[str, object]:
        async with self._lock:
            await self.restore()
            chain_id = int(await self.w3.eth.chain_id)
            if chain_id != 8453:
                raise RuntimeError(f"pool discovery requires Base mainnet chainId 8453, got {chain_id}")
            latest = int(await self.w3.eth.block_number)
            stats = DiscoveryStats()
            await self.discover_aerodrome(stats)
            await self.discover_uniswap(stats, latest)
            await self.discover_slipstream(stats, latest)
            return {
                "latest_block": latest,
                "aerodrome_cursor": self._aerodrome_cursor,
                "uniswap_cursor": self._uniswap_cursor,
                "slipstream_cursor": self._slipstream_cursor,
                "stats": stats.as_dict(),
                "pool_count": self.graph.snapshot()["pool_count"],
            }
