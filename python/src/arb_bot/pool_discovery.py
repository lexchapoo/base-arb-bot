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


BASE_CHAIN_ID = 8453


class _NullAsyncLock:
    """No-op stand-in so the commit guard is uniform when no lock was supplied."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc) -> bool:
        return False


_NULL_LOCK = _NullAsyncLock()


class PoolVerificationError(RuntimeError):
    """A pool could not be verified for a reason that may not be permanent.

    Deliberately not a ValueError: the discovery loops treat ValueError as a
    deterministic rejection and let the cursor move past it, while any other exception
    holds the cursor for a bounded number of retries. Anything that could be caused by
    the *endpoint* rather than the pool must therefore not be a ValueError.
    """


class PoolCodeMissingError(PoolVerificationError):
    """A factory-announced pool has no code at the verification endpoint.

    When raised out of refresh() it also carries what that pass *did* accomplish:
    `partial` is the result dict assembled before the blocker was checked, and `blockers`
    maps each held source to its detail. A blocker holds one source's cursor and says
    nothing about the other two, which may have reached head on the same pass -- a caller
    that discards the whole result therefore reports no progress at all for sources that
    are in fact fine. Both default to empty for the low-level raise in _require_code.
    """

    def __init__(
        self,
        *args,
        partial: dict[str, object] | None = None,
        blockers: dict[str, str] | None = None,
    ):
        super().__init__(*args)
        self.partial = dict(partial) if partial is not None else None
        self.blockers = dict(blockers or {})


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
        call_rpc_url: str = "",
        max_call_endpoint_lag_blocks: int = 64,
        commit_lock=None,
    ) -> None:
        if AsyncWeb3 is None or AsyncHTTPProvider is None:
            raise RuntimeError("web3 is required for automatic pool discovery; run `make setup`")
        self.w3 = AsyncWeb3(AsyncHTTPProvider(rpc_url))
        # Log reads and verification calls have different constraints. eth_getLogs needs an
        # endpoint with history (a pruned node answers 4444), while the per-pool
        # get_code/token0/factory checks are latest-block eth_calls that any synced node can
        # serve. Splitting them lets a rate-limited archive endpoint handle only the log
        # scan while a local node absorbs the verification fan-out, which is what actually
        # trips rate limits. Defaults to the same endpoint, so behaviour is unchanged unless set.
        self.call_w3 = AsyncWeb3(AsyncHTTPProvider(call_rpc_url)) if call_rpc_url else self.w3
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
        # How far the verification endpoint may trail the log endpoint before its answers
        # are treated as untrustworthy. A lagging node has not yet seen recently created
        # pools, so get_code() on them returns empty and they look like they do not exist.
        self.max_call_endpoint_lag_blocks = max(0, int(max_call_endpoint_lag_blocks))
        self._endpoints_verified = False
        # Sources currently blocked on a pool the verification endpoint says has no code,
        # as source -> (blocked cursor position, detail). The position is what makes the
        # blocker resolvable: it says which index/block must come back verified before the
        # source is unblocked. Never cleared by the retry budget: see _note_code_missing.
        self._code_missing: dict[str, tuple[int, str]] = {}
        # Held across "persist this batch, register it, advance the cursor" -- see
        # _commit_guard. The service passes the same lock its curation handlers use.
        self.commit_lock = commit_lock

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

    def _code_missing_state(self) -> dict[str, tuple[int, str]]:
        """Per-instance code-missing blockers, created on demand (see _stall_state)."""
        blocked = self.__dict__.get("_code_missing")
        if blocked is None:
            blocked = {}
            self._code_missing = blocked
        return blocked

    def _note_code_missing(self, source: str, cursor: int, detail: str) -> None:
        """Record a blocker at `cursor` that the bounded retry budget must never clear.

        A transient RPC error and a deterministic metadata rejection are both survivable:
        the first resolves itself, the second is a real "this is not a pool we want". An
        empty get_code on an address the factory announced is neither. It means the
        verification endpoint is wrong about the world, and skipping past it drops a real
        pool permanently while every downstream signal -- cursor at head, scan "complete",
        exit code 0 -- says the scan succeeded. So it holds the cursor indefinitely and is
        raised out of refresh() instead of being spent against `max_cursor_retries`.

        `cursor` is the position being held. It is not decoration: while a blocker stands,
        callers must refuse to spend the retry budget at all (a source that alternates
        between "no code" and "timed out" would otherwise exhaust the budget on the timeout
        passes and skip the very pool the blocker exists to protect), and the blocker may
        only be cleared once that position has been re-examined and come back verified.
        """
        self._code_missing_state()[source] = (int(cursor), detail)

    def _clear_code_missing(self, source: str) -> None:
        self._code_missing_state().pop(source, None)

    def _code_missing_cursor(self, source: str) -> int | None:
        """The position `source` is held at, or None when it is not blocked."""
        held = self._code_missing_state().get(source)
        return None if held is None else held[0]

    def code_missing_blockers(self) -> dict[str, str]:
        """Sources currently held by an unverifiable (code-absent) pool."""
        return {source: detail for source, (_cursor, detail) in self._code_missing_state().items()}


    def _commit_guard(self):
        """Serialise this batch's persist-and-register against curation.

        Discovery persists a pool, registers it, and advances its cursor past it. Those
        three are one step: if a curated restriction is lifted between the persist and the
        register, the register is still filtered by the restriction that is on its way out
        while the rebuild that replaces it worked from a registry snapshot taken before
        the persist. The pool then exists in `verified_pools`, is absent from the graph,
        and the cursor has moved past it -- so nothing puts it back until a restart.

        The lock is supplied by the service (the same one its curation handlers take), so
        a batch either lands entirely before a lift takes its snapshot or entirely after
        the swap, when the graph is already unrestricted.
        """
        return self.__dict__.get("commit_lock") or _NULL_LOCK

    def admits(self, pool: PoolMetadata) -> bool:
        """Whether the configured token allowlist lets this pool into the graph.

        Public so callers rebuilding the graph from the durable registry apply exactly the
        same filter restore() does, instead of reimplementing it slightly differently.
        """
        return _allow(pool, self.__dict__.get("token_allowlist") or set())

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
            # Not under _commit_guard: restore() only ever re-registers pools that are
            # already in `verified_pools` and it advances no cursor, so a selection landing
            # mid-restore costs nothing -- lifting that selection rebuilds from the same
            # table. The guard exists for the case where the cursor moves past a pool.
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

    async def verify_endpoint_consistency(self, *, force: bool = False) -> dict[str, int]:
        """Prove the log and verification endpoints describe the same, current chain.

        With `call_rpc_url` pointed at a second node, every existence and metadata check
        runs somewhere the log scan never touches. If that node is on the
        wrong chain -- or simply behind -- `get_code()` on a freshly created pool comes
        back empty, verification declines the pool, and the cursor advances past it as
        though it had been examined. The pool is then invisible until the table is wiped.
        Nothing downstream can detect that, so it has to be caught here.

        Cached after the first success: the endpoints do not change mid-process, and this
        costs two round trips.
        """
        call_w3 = self.__dict__.get("call_w3")
        log_w3 = self.__dict__.get("w3")
        if call_w3 is None or log_w3 is None or call_w3 is log_w3:
            # Single-endpoint discovery: the log scan and the verification calls cannot
            # disagree with each other, and refresh() already pins the chain id.
            return {}
        if self.__dict__.get("_endpoints_verified") and not force:
            return {}
        log_chain, call_chain = await asyncio.gather(log_w3.eth.chain_id, call_w3.eth.chain_id)
        if int(call_chain) != int(log_chain):
            raise RuntimeError(
                f"pool discovery endpoints disagree on chain: logs={int(log_chain)} "
                f"calls={int(call_chain)}; verification would silently reject valid pools"
            )
        if int(call_chain) != BASE_CHAIN_ID:
            raise RuntimeError(
                f"pool discovery requires Base mainnet chainId {BASE_CHAIN_ID}, got {int(call_chain)}"
            )
        log_head, call_head = await asyncio.gather(log_w3.eth.block_number, call_w3.eth.block_number)
        lag = int(log_head) - int(call_head)
        max_lag = int(self.__dict__.get("max_call_endpoint_lag_blocks", 64))
        if lag > max_lag:
            raise RuntimeError(
                f"pool discovery call endpoint is {lag} blocks behind the log endpoint "
                f"(log_head={int(log_head)} call_head={int(call_head)}, max {max_lag}); "
                "recently created pools would be rejected as non-existent"
            )
        self._endpoints_verified = True
        return {"log_head": int(log_head), "call_head": int(call_head), "call_endpoint_lag": lag}

    async def _has_code(self, address: str) -> bool:
        code = await self.call_w3.eth.get_code(AsyncWeb3.to_checksum_address(address))
        return bool(code and bytes(code) != b"\x00")

    async def _require_code(self, address: str) -> None:
        """Raise when a factory-announced pool has no code at the verification endpoint.

        A pool the factory emitted a PoolCreated for (or indexed in allPools) has code by
        construction, so an empty result means the endpoint is wrong about the world, not
        that the pool is absent. Raising holds the cursor indefinitely: the per-source
        retry budget is deliberately *not* consulted while the resulting blocker stands,
        because giving up here would drop a real pool while every downstream signal reports
        a clean, complete scan. Recovery is a pass in which the held position verifies.
        """
        if await self._has_code(address):
            return
        raise PoolCodeMissingError(
            f"no deployed code at {address} on the verification endpoint"
        )

    async def _verify_aerodrome(self, address: str) -> PoolMetadata | None:
        await self._require_code(address)
        c = self.call_w3.eth.contract(address=AsyncWeb3.to_checksum_address(address), abi=AERODROME_POOL_ABI)
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
        await self.verify_endpoint_consistency()
        if not await self._has_code(self.aerodrome_factory):
            raise RuntimeError("configured Aerodrome factory has no deployed code on Base")
        factory = self.w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(self.aerodrome_factory), abi=AERODROME_FACTORY_ABI
        )
        total = int(await factory.functions.allPoolsLength().call())
        scan_start = self._aerodrome_cursor
        indexes = list(range(scan_start, total))
        if not indexes:
            return
        sem = asyncio.Semaphore(self.concurrency)
        discovered: list[PoolMetadata] = []
        failed: set[int] = set()
        codeless: dict[int, str] = {}

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
                except PoolCodeMissingError:
                    # Not "this pool does not exist" -- the factory indexed it, so an empty
                    # get_code means the verification endpoint is wrong about the world.
                    stats.rejected_no_code += 1
                    failed.add(i)
                    codeless[i] = address
                    return
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
        # Resolve a standing blocker first. Unlike the chunked log scans, this pass examines
        # *every* remaining index, so the held index can come back verified while some later
        # index fails and takes over the cursor. Without this the blocker outlived the
        # condition that caused it: refresh() kept raising -- and discarding its result, so
        # /ready never recorded a completed first scan -- over an index that was long fine.
        # "Verified" means examined and not re-flagged: an index that merely timed out again
        # tells us nothing new, so it must not count as resolution.
        blocked_at = self._code_missing_cursor("aerodrome-v2")
        if (
            blocked_at is not None
            and scan_start <= blocked_at < total
            and blocked_at not in codeless
            and blocked_at not in failed
        ):
            self._clear_code_missing("aerodrome-v2")
        if failed:
            if next_cursor in codeless:
                # Unskippable: the retry budget is not consulted, so it cannot be exhausted
                # and cannot advance the cursor past a pool that was never examined.
                self._note_code_missing(
                    "aerodrome-v2",
                    next_cursor,
                    f"allPools index {next_cursor} ({codeless[next_cursor]}) has no code "
                    "at the verification endpoint",
                )
            # A pool that fails deterministically (reverting token0(), self-destructed code,
            # malformed metadata) would otherwise pin the cursor forever and make every
            # refresh re-scan the entire tail. Retry it a bounded number of times, then skip.
            # Not while a blocker stands, though: an endpoint that flaps between "no code"
            # and "timed out" only reports codeless on some passes, and spending the budget
            # on the others would step the cursor past the announced pool anyway.
            elif self._code_missing_cursor("aerodrome-v2") is None and self._retry_budget_exhausted(
                "aerodrome-v2", next_cursor
            ):
                stats.rejected_metadata += 1
                self._clear_stall("aerodrome-v2")
                next_cursor = min(failed) + 1
        else:
            self._clear_stall("aerodrome-v2")
            self._clear_code_missing("aerodrome-v2")
        async with self._commit_guard():
            if self.store is not None:
                await self.store.save_batch(
                    source="aerodrome-v2", factory=self.aerodrome_factory,
                    next_cursor=next_cursor, pools=discovered,
                )
            # Counts entries into the routable graph, not rows persisted: a standing
            # curated restriction turns most discovered pools away, and counting those
            # made `registered` disagree with pool_count by orders of magnitude.
            for pool in discovered:
                if self.graph.register(pool):
                    stats.registered += 1
            self._aerodrome_cursor = next_cursor

    async def _verify_uniswap(self, *, address: str, token0: str, token1: str, fee: int) -> PoolMetadata | None:
        await self._require_code(address)
        c = self.call_w3.eth.contract(address=AsyncWeb3.to_checksum_address(address), abi=UNISWAP_POOL_ABI)
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
        await self.verify_endpoint_consistency()
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
            chunk_codeless: list[str] = []
            # Verify concurrently, as discover_aerodrome already does. Each pool costs
            # several latest-block eth_calls (get_code/token0/token1/fee/factory); doing
            # them one await at a time serialises the whole scan and dominates runtime
            # once pool density rises. Ordering does not matter -- results are collected
            # into `discovered` and committed together with the cursor below.
            sem = asyncio.Semaphore(self.concurrency)

            async def _one(raw, chunk_codeless=chunk_codeless) -> None:
                nonlocal chunk_failed
                async with sem:
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
                    except PoolCodeMissingError as exc:
                        # The factory emitted PoolCreated for this address, so it has code by
                        # construction. An empty get_code means the verification endpoint is
                        # behind or on another chain -- re-scan rather than skip the pool.
                        stats.rejected_no_code += 1
                        chunk_failed = True
                        chunk_codeless.append(str(exc))
                    except ValueError:
                        # Deterministic rejection (factory/token/fee mismatch): re-scanning
                        # would reject it again, so let the cursor move past it.
                        stats.rejected_metadata += 1
                    except Exception:
                        # Transient failure (RPC error, timeout): re-scan this chunk.
                        stats.rejected_metadata += 1
                        chunk_failed = True

            await asyncio.gather(*(_one(raw) for raw in logs))
            # Hold the cursor on a chunk that had transient failures so nothing is skipped --
            # but only for a bounded number of passes. A log that fails deterministically
            # (a pool whose factory()/token0() reverts, raising something other than
            # ValueError) would otherwise stall discovery at this block range permanently.
            if chunk_codeless:
                # Unskippable (see _note_code_missing): hold this chunk indefinitely rather
                # than letting the retry budget advance past an announced pool.
                self._note_code_missing(
                    "uniswap-v3",
                    start,
                    f"blocks {start}-{end}: {len(chunk_codeless)} announced pool(s) with no "
                    f"code at the verification endpoint (first: {chunk_codeless[0]})",
                )
            # The budget may not be spent while a blocker stands. An endpoint that flaps
            # between "no code" and "timed out" reports codeless on only some passes, so
            # counting the others against `max_cursor_retries` would exhaust it and advance
            # `next_cursor` past a chunk holding an announced pool that never verified --
            # the exact skip the blocker exists to prevent. Only a clean pass clears it.
            elif (
                chunk_failed
                and self._code_missing_cursor("uniswap-v3") is None
                and self._retry_budget_exhausted("uniswap-v3", start)
            ):
                chunk_failed = False
                self._clear_stall("uniswap-v3")
            elif not chunk_failed:
                self._clear_stall("uniswap-v3")
                self._clear_code_missing("uniswap-v3")
            next_cursor = start if chunk_failed else end + 1
            async with self._commit_guard():
                if self.store is not None:
                    await self.store.save_batch(
                        source="uniswap-v3", factory=self.uniswap_v3_factory,
                        next_cursor=next_cursor, pools=discovered, verified_block=end,
                    )
                # Counts entries into the routable graph, not rows persisted: a standing
                # curated restriction turns most discovered pools away, and counting those
                # made `registered` disagree with pool_count by orders of magnitude.
                for pool in discovered:
                    if self.graph.register(pool):
                        stats.registered += 1
                self._uniswap_cursor = next_cursor
            if chunk_failed:
                # Stop this pass rather than re-reading the same range in a tight loop; the
                # next scheduled refresh resumes from the un-advanced cursor.
                return
            start = next_cursor

    async def _verify_slipstream(self, *, address: str, token0: str, token1: str, tick_spacing: int) -> PoolMetadata | None:
        await self._require_code(address)
        c = self.call_w3.eth.contract(address=AsyncWeb3.to_checksum_address(address), abi=SLIPSTREAM_POOL_ABI)
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
        await self.verify_endpoint_consistency()
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
            chunk_codeless: list[str] = []
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
                except PoolCodeMissingError as exc:
                    stats.rejected_no_code += 1
                    chunk_failed = True
                    chunk_codeless.append(str(exc))
                except ValueError:
                    stats.rejected_metadata += 1
                except Exception:
                    stats.rejected_metadata += 1
                    chunk_failed = True
            if chunk_codeless:
                self._note_code_missing(
                    "aerodrome-slipstream",
                    start,
                    f"blocks {start}-{end}: {len(chunk_codeless)} announced pool(s) with no "
                    f"code at the verification endpoint (first: {chunk_codeless[0]})",
                )
            # The budget may not be spent while a blocker stands. An endpoint that flaps
            # between "no code" and "timed out" reports codeless on only some passes, so
            # counting the others against `max_cursor_retries` would exhaust it and advance
            # `next_cursor` past a chunk holding an announced pool that never verified --
            # the exact skip the blocker exists to prevent. Only a clean pass clears it.
            elif (
                chunk_failed
                and self._code_missing_cursor("aerodrome-slipstream") is None
                and self._retry_budget_exhausted("aerodrome-slipstream", start)
            ):
                chunk_failed = False
                self._clear_stall("aerodrome-slipstream")
            elif not chunk_failed:
                self._clear_stall("aerodrome-slipstream")
                self._clear_code_missing("aerodrome-slipstream")
            next_cursor = start if chunk_failed else end + 1
            async with self._commit_guard():
                if self.store is not None:
                    await self.store.save_batch(
                        source="aerodrome-slipstream", factory=self.slipstream_factory,
                        next_cursor=next_cursor, pools=discovered, verified_block=end,
                    )
                # Counts entries into the routable graph, not rows persisted: a standing
                # curated restriction turns most discovered pools away, and counting those
                # made `registered` disagree with pool_count by orders of magnitude.
                for pool in discovered:
                    if self.graph.register(pool):
                        stats.registered += 1
                self._slipstream_cursor = next_cursor
            if chunk_failed:
                return
            start = next_cursor

    async def refresh(self) -> dict[str, object]:
        async with self._lock:
            await self.restore()
            chain_id = int(await self.w3.eth.chain_id)
            if chain_id != BASE_CHAIN_ID:
                raise RuntimeError(
                    f"pool discovery requires Base mainnet chainId {BASE_CHAIN_ID}, got {chain_id}"
                )
            # Re-checked every pass, not just at construction: the call endpoint can fall
            # behind (or be swapped behind a load balancer) long after startup.
            endpoints = await self.verify_endpoint_consistency(force=True)
            latest = int(await self.w3.eth.block_number)
            stats = DiscoveryStats()
            # All three run before any code-absence blocker is raised: one unverifiable
            # Uniswap pool must not starve Aerodrome and Slipstream discovery.
            await self.discover_aerodrome(stats)
            await self.discover_uniswap(stats, latest)
            await self.discover_slipstream(stats, latest)
            result = {
                "latest_block": latest,
                "aerodrome_cursor": self._aerodrome_cursor,
                "uniswap_cursor": self._uniswap_cursor,
                "slipstream_cursor": self._slipstream_cursor,
                "stats": stats.as_dict(),
                "pool_count": self.graph.snapshot()["pool_count"],
                **endpoints,
            }
            blockers = self.code_missing_blockers()
            if blockers:
                # Loud, and every pass: the cursors above are held, so this is not a partial
                # success to be logged once and forgotten -- discovery cannot progress past
                # these sources until the verification endpoint can see the pools.
                raise PoolCodeMissingError(
                    "discovery is blocked on announced pools with no code at the "
                    "verification endpoint: "
                    + "; ".join(f"{source}: {detail}" for source, detail in sorted(blockers.items())),
                    # The cursors, stats and pool count above are real work, and the sources
                    # that are not in `blockers` may have reached head on this very pass.
                    # Hand them to the caller rather than making one held source look like
                    # a total failure.
                    partial=result,
                    blockers=blockers,
                )
            return result
