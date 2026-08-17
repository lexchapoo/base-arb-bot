from __future__ import annotations

import json
from dataclasses import dataclass
from threading import RLock


@dataclass(frozen=True, slots=True)
class PoolMetadata:
    address: str
    token0: str
    token1: str
    venue: str
    pool_type: str
    fee: int | None = None
    stable: bool | None = None

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
    ) -> "PoolMetadata":
        return cls(
            address=cls.normalize_address(address),
            token0=cls.normalize_address(token0),
            token1=cls.normalize_address(token1),
            venue=venue.strip().lower(),
            pool_type=pool_type.strip().lower(),
            fee=fee,
            stable=stable,
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

    def register(self, pool: PoolMetadata) -> None:
        with self._lock:
            previous = self._pools.get(pool.address)
            if previous:
                for token in (previous.token0, previous.token1):
                    self._token_to_pools.get(token, set()).discard(previous.address)
            self._pools[pool.address] = pool
            for token in (pool.token0, pool.token1):
                self._token_to_pools.setdefault(token, set()).add(pool.address)

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

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "pool_count": len(self._pools),
                "token_count": len(self._token_to_pools),
                "pools": [
                    {
                        "address": p.address,
                        "token0": p.token0,
                        "token1": p.token1,
                        "venue": p.venue,
                        "pool_type": p.pool_type,
                        "fee": p.fee,
                        "stable": p.stable,
                    }
                    for p in sorted(self._pools.values(), key=lambda x: x.address)
                ],
            }


def load_configured_pools(raw_json: str, watched_addresses: str = "") -> list[PoolMetadata]:
    """Parse operator-supplied, chain-verified pool metadata and bind it to the watchlist."""
    if not raw_json.strip():
        return []
    parsed = json.loads(raw_json)
    if not isinstance(parsed, list):
        raise ValueError("WATCHED_POOLS_JSON must be a JSON array")

    pools: list[PoolMetadata] = []
    seen: set[str] = set()
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError("every WATCHED_POOLS_JSON entry must be an object")
        pool = PoolMetadata.create(
            address=str(item.get("address", "")),
            token0=str(item.get("token0", "")),
            token1=str(item.get("token1", "")),
            venue=str(item.get("venue", "")),
            pool_type=str(item.get("pool_type", "")),
            fee=item.get("fee"),
            stable=item.get("stable"),
        )
        if pool.address in seen:
            raise ValueError(f"duplicate configured pool: {pool.address}")
        seen.add(pool.address)
        pools.append(pool)

    watched = {
        PoolMetadata.normalize_address(value)
        for value in watched_addresses.split(",")
        if value.strip()
    }
    if watched and watched != seen:
        missing_metadata = sorted(watched - seen)
        unwatched_metadata = sorted(seen - watched)
        raise ValueError(
            "WATCHED_POOL_ADDRESSES and WATCHED_POOLS_JSON differ: "
            f"missing_metadata={missing_metadata}, unwatched_metadata={unwatched_metadata}"
        )
    return pools
