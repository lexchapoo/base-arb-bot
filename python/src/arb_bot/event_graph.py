from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from threading import RLock


def canonical_venue(venue: str) -> str:
    """Collapse a venue label onto the adapter key that actually quotes it.

    Lives here rather than on AdapterRegistry so the API request models can apply the
    same mapping without importing the route engine (and with it every adapter).
    """
    v = venue.strip().lower()
    if "slipstream" in v:
        return "aerodrome-slipstream"
    if "uniswap" in v:
        return "uniswap-v3"
    if "aerodrome" in v:
        return "aerodrome"
    return v


@dataclass(frozen=True, slots=True)
class PoolMetadata:
    address: str
    token0: str
    token1: str
    venue: str
    pool_type: str
    fee: int | None = None
    stable: bool | None = None
    tick_spacing: int | None = None

    @staticmethod
    def normalize_address(value: str) -> str:
        value = value.strip().lower()
        if not value.startswith("0x") or len(value) != 42:
            raise ValueError(f"invalid EVM address: {value}")
        int(value[2:], 16)
        return value

    @classmethod
    def create(
        cls,
        *,
        address: str,
        token0: str,
        token1: str,
        venue: str,
        pool_type: str,
        fee: int | None = None,
        stable: bool | None = None,
        tick_spacing: int | None = None,
    ) -> "PoolMetadata":
        return cls(
            address=cls.normalize_address(address),
            token0=cls.normalize_address(token0),
            token1=cls.normalize_address(token1),
            venue=venue.strip().lower(),
            pool_type=pool_type.strip().lower(),
            fee=fee,
            stable=stable,
            tick_spacing=None if tick_spacing is None else int(tick_spacing),
        )


@dataclass(frozen=True, slots=True)
class Cycle:
    pools: tuple[PoolMetadata, ...]
    tokens: tuple[str, ...]

    @property
    def hops(self) -> int:
        return len(self.pools)

    @property
    def key(self) -> str:
        return "|".join(p.address for p in self.pools) + ":" + "|".join(self.tokens)


class LivePoolGraph:
    """Thread-safe pool/token adjacency index populated only from discovered production metadata."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._pools: dict[str, PoolMetadata] = {}
        self._token_to_pools: dict[str, set[str]] = {}
        # When set, the graph admits only these addresses -- see restrict_to().
        self._allowed: set[str] | None = None

    def restrict_to(self, addresses: Iterable[str] | None) -> int:
        """Confine the graph to `addresses`, dropping anything already outside them.

        Curation and automatic discovery both write into this one graph, and discovery is
        on by default. Without a standing restriction a curated selection only held until
        the next refresh put every verified pool back -- so "replace the pool set" was true
        for a few seconds and then quietly false. Making the selection a filter rather than
        a one-shot swap means discovery keeps its job (finding and persisting pools) while
        the operator keeps theirs (deciding which ones the router runs on).

        Pass None to lift the restriction. Note that lifting alone does not bring back the
        pools that were turned away while it was up -- they were never stored anywhere in
        this object, and discovery's cursors have long since advanced past them. Callers
        that lift a restriction must repopulate from the durable registry; replace_all(...,
        restrict=False) does both in one step.
        """
        with self._lock:
            if addresses is None:
                self._allowed = None
                return len(self._pools)
            allowed = {PoolMetadata.normalize_address(a) for a in addresses}
            self._allowed = allowed
            for address in [a for a in self._pools if a not in allowed]:
                self._drop_locked(address)
            return len(self._pools)

    def restriction(self) -> set[str] | None:
        with self._lock:
            return None if self._allowed is None else set(self._allowed)

    def admit(self, pool: PoolMetadata) -> None:
        """Register a pool and widen any standing restriction to include it.

        The manual counterpart to register(): an operator adding one pool is expressing
        the selection, not being filtered by it.
        """
        with self._lock:
            if self._allowed is not None:
                self._allowed.add(pool.address)
            self.register(pool)

    def register(self, pool: PoolMetadata) -> bool:
        """Add a pool to the routable graph. False when a restriction turned it away.

        The return value is what lets callers count what they actually registered. Under a
        400-pool selection this is a no-op for most of the 28.6k pools discovery verifies,
        and a caller incrementing a counter unconditionally reported tens of thousands of
        registrations next to a pool_count of 400.
        """
        with self._lock:
            if self._allowed is not None and pool.address not in self._allowed:
                # Discovery found it and the registry persisted it; it is simply not part
                # of the current selection, so it must not enter the routable graph.
                return False
            previous = self._pools.get(pool.address)
            if previous:
                for token in (previous.token0, previous.token1):
                    self._token_to_pools.get(token, set()).discard(previous.address)
            self._pools[pool.address] = pool
            for token in (pool.token0, pool.token1):
                self._token_to_pools.setdefault(token, set()).add(pool.address)
            return True

    def _drop_locked(self, key: str) -> bool:
        """Remove one pool and its token adjacency. Caller must hold the lock."""
        previous = self._pools.pop(key, None)
        if previous is None:
            return False
        for token in (previous.token0, previous.token1):
            holders = self._token_to_pools.get(token)
            if holders is None:
                continue
            holders.discard(key)
            if not holders:
                del self._token_to_pools[token]
        return True

    def unregister(self, pool_address: str) -> bool:
        """Drop a pool from the graph. Returns False if it was not registered.

        Also narrows any standing restriction, so a later discovery refresh cannot
        reinstate the pool that was just removed.
        """
        try:
            key = PoolMetadata.normalize_address(pool_address)
        except ValueError:
            return False
        with self._lock:
            if self._allowed is not None:
                self._allowed.discard(key)
            return self._drop_locked(key)

    def replace_all(self, pools: Iterable[PoolMetadata], *, restrict: bool = True) -> int:
        """Atomically make `pools` the entire graph.

        Registration alone is additive, so a curated selection could only ever grow:
        re-running curation with a tighter floor left every previously registered pool
        in the graph. Swapping the whole index under one lock makes a selection
        authoritative instead of cumulative, and no reader observes a half-built graph.

        `restrict=True` also installs the selection as a standing filter (see restrict_to),
        so a concurrent or subsequent discovery refresh cannot add the dropped pools back.
        `restrict=False` lifts any standing filter and hands the graph to `pools` as-is --
        the way to give the graph back to discovery, since it repopulates and unfilters
        together rather than leaving a gap where neither is true.
        """
        materialised = list(pools)
        index: dict[str, PoolMetadata] = {}
        tokens: dict[str, set[str]] = {}
        for pool in materialised:
            index[pool.address] = pool
            for token in (pool.token0, pool.token1):
                tokens.setdefault(token, set()).add(pool.address)
        with self._lock:
            self._pools = index
            self._token_to_pools = tokens
            self._allowed = set(index) if restrict else None
            return len(index)

    def get(self, pool_address: str) -> PoolMetadata | None:
        try:
            key = PoolMetadata.normalize_address(pool_address)
        except ValueError:
            return None
        with self._lock:
            return self._pools.get(key)

    def contains(self, pool_address: str) -> bool:
        return self.get(pool_address) is not None

    def pools_for_token(self, token: str) -> list[PoolMetadata]:
        key = PoolMetadata.normalize_address(token)
        with self._lock:
            return [self._pools[p] for p in sorted(self._token_to_pools.get(key, set()))]

    def pools_by_addresses(self, addresses: list[str]) -> list[PoolMetadata]:
        with self._lock:
            out: list[PoolMetadata] = []
            for raw in addresses:
                try:
                    key = PoolMetadata.normalize_address(raw)
                except ValueError:
                    continue
                if key in self._pools:
                    out.append(self._pools[key])
            return out

    def affected_subgraph(self, changed_pool: str) -> dict[str, object]:
        key = PoolMetadata.normalize_address(changed_pool)
        with self._lock:
            pool = self._pools.get(key)
            if pool is None:
                return {
                    "known_pool": False,
                    "changed_pool": key,
                    "affected_tokens": [],
                    "affected_pools": [],
                }
            tokens = {pool.token0, pool.token1}
            affected: set[str] = {key}
            for token in tokens:
                affected.update(self._token_to_pools.get(token, set()))
            return {
                "known_pool": True,
                "changed_pool": key,
                "affected_tokens": sorted(tokens),
                "affected_pools": sorted(affected),
            }

    @staticmethod
    def _other_token(pool: PoolMetadata, token: str) -> str | None:
        if pool.token0 == token:
            return pool.token1
        if pool.token1 == token:
            return pool.token0
        return None

    def cycles_for_affected(self, affected_addresses: list[str], max_hops: int = 3) -> list[Cycle]:
        """Enumerate only 2/3-hop cycles touching the affected pool set.

        No market state is invented here. This operates solely on registered live pool metadata.
        """
        affected = {PoolMetadata.normalize_address(a) for a in affected_addresses}
        with self._lock:
            pools = dict(self._pools)
            token_to_pools = {k: set(v) for k, v in self._token_to_pools.items()}

        cycles: dict[str, Cycle] = {}
        for start_pool_addr in sorted(affected):
            p1 = pools.get(start_pool_addr)
            if not p1:
                continue
            for start_token in (p1.token0, p1.token1):
                mid = self._other_token(p1, start_token)
                if not mid:
                    continue
                # 2-hop: same token pair through another pool/venue.
                for p2_addr in sorted(token_to_pools.get(mid, set())):
                    if p2_addr == p1.address:
                        continue
                    p2 = pools[p2_addr]
                    if self._other_token(p2, mid) == start_token:
                        c = Cycle((p1, p2), (start_token, mid, start_token))
                        cycles[c.key] = c
                if max_hops < 3:
                    continue
                # 3-hop: A->B, B->C, C->A.
                for p2_addr in sorted(token_to_pools.get(mid, set())):
                    if p2_addr == p1.address:
                        continue
                    p2 = pools[p2_addr]
                    third = self._other_token(p2, mid)
                    if not third or third == start_token:
                        continue
                    for p3_addr in sorted(token_to_pools.get(third, set())):
                        if p3_addr in {p1.address, p2.address}:
                            continue
                        p3 = pools[p3_addr]
                        if self._other_token(p3, third) == start_token:
                            c = Cycle((p1, p2, p3), (start_token, mid, third, start_token))
                            cycles[c.key] = c
        return list(cycles.values())

    def addresses(self) -> list[str]:
        with self._lock:
            return sorted(self._pools)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "pool_count": len(self._pools),
                "token_count": len(self._token_to_pools),
                # None means "discovery populates freely"; a number means a curated
                # selection is filtering it.
                "restricted_to": None if self._allowed is None else len(self._allowed),
                "pools": [
                    {
                        "address": p.address,
                        "token0": p.token0,
                        "token1": p.token1,
                        "venue": p.venue,
                        "pool_type": p.pool_type,
                        "fee": p.fee,
                        "stable": p.stable,
                        "tick_spacing": p.tick_spacing,
                    }
                    for p in sorted(self._pools.values(), key=lambda x: x.address)
                ],
            }
