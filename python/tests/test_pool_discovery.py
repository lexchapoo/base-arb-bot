from arb_bot.config import Settings
from arb_bot.event_graph import LivePoolGraph, PoolMetadata
from arb_bot.pool_discovery import _allow


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def test_official_base_factory_defaults_are_present():
    cfg = Settings(_env_file=None)
    assert cfg.chain_id == 8453
    assert cfg.auto_pool_discovery_enabled is True
    assert cfg.aerodrome_factory.lower() == "0x420dd381b31aef6683db6b902084cb0ffece40da"
    assert cfg.uniswap_v3_factory.lower() == "0x33128a8fc17869897dce68ed026d694621f6fdfd"


def test_discovered_graph_generates_sorted_watched_addresses():
    graph = LivePoolGraph()
    graph.register(PoolMetadata.create(address=addr(30), token0=addr(1), token1=addr(2), venue="aerodrome", pool_type="xyk"))
    graph.register(PoolMetadata.create(address=addr(20), token0=addr(1), token1=addr(3), venue="uniswap-v3", pool_type="concentrated", fee=500))
    assert graph.addresses() == [addr(20), addr(30)]


def test_optional_token_allowlist_filters_without_inventing_market_state():
    pool = PoolMetadata.create(address=addr(30), token0=addr(1), token1=addr(2), venue="aerodrome", pool_type="xyk")
    assert _allow(pool, set()) is True
    assert _allow(pool, {addr(1)}) is True
    assert _allow(pool, {addr(99)}) is False

import asyncio
from arb_bot.pool_discovery import BasePoolDiscovery


class FakeRegistryStore:
    def __init__(self, pools, cursors):
        self.pools = list(pools)
        self.cursors = dict(cursors)

    async def load_pools(self):
        return list(self.pools)

    async def load_cursor(self, source, factory, default):
        return self.cursors.get(source, default)

    async def save_batch(self, **kwargs):
        raise AssertionError("restore must not write")


def test_restart_restore_rehydrates_graph_and_factory_cursors():
    graph = LivePoolGraph()
    pool = PoolMetadata.create(
        address=addr(55), token0=addr(1), token1=addr(2),
        venue="uniswap-v3", pool_type="concentrated", fee=500,
    )
    discovery = object.__new__(BasePoolDiscovery)
    discovery.graph = graph
    discovery.aerodrome_factory = addr(101)
    discovery.uniswap_v3_factory = addr(102)
    discovery.slipstream_factory = ""
    discovery.uniswap_from_block = 1000
    discovery.token_allowlist = set()
    discovery._aerodrome_cursor = 0
    discovery._uniswap_cursor = 1000
    discovery._slipstream_cursor = 1000
    discovery._restored = False
    discovery.store = FakeRegistryStore(
        [pool], {"aerodrome-v2": 27, "uniswap-v3": 4567}
    )

    result = asyncio.run(discovery.restore())

    assert result["restored_pools"] == 1
    assert graph.contains(addr(55))
    assert discovery._aerodrome_cursor == 27
    assert discovery._uniswap_cursor == 4567


def test_restore_is_idempotent_and_does_not_duplicate_graph_entries():
    graph = LivePoolGraph()
    pool = PoolMetadata.create(
        address=addr(56), token0=addr(1), token1=addr(3),
        venue="aerodrome", pool_type="xyk", stable=False,
    )
    discovery = object.__new__(BasePoolDiscovery)
    discovery.graph = graph
    discovery.aerodrome_factory = addr(101)
    discovery.uniswap_v3_factory = addr(102)
    discovery.slipstream_factory = ""
    discovery.uniswap_from_block = 0
    discovery.token_allowlist = set()
    discovery._aerodrome_cursor = 0
    discovery._uniswap_cursor = 0
    discovery._slipstream_cursor = 0
    discovery._restored = False
    discovery.store = FakeRegistryStore([pool], {})

    first = asyncio.run(discovery.restore())
    second = asyncio.run(discovery.restore())

    assert first["restored_pools"] == 1
    assert second["restored_pools"] == 0
    assert graph.snapshot()["pool_count"] == 1


def test_poolcreated_topic_filter_is_0x_prefixed_32_bytes():
    """hexbytes>=1.0 returns unprefixed hex from .hex(); eth_getLogs rejects that as a topic."""
    from web3 import AsyncWeb3
    from web3.providers import AsyncHTTPProvider

    w3 = AsyncWeb3(AsyncHTTPProvider("http://localhost:1"))
    digest = w3.keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
    topic0 = digest if digest.startswith("0x") else f"0x{digest}"
    assert topic0.startswith("0x")
    assert len(topic0) == 66
    assert int(topic0, 16) > 0


class _RecordingStore:
    def __init__(self):
        self.saved = []

    async def load_pools(self):
        return []

    async def load_cursor(self, source, factory, default):
        return default

    async def save_batch(self, *, source, factory, next_cursor, pools, verified_block=None):
        self.saved.append((source, next_cursor, [p.address for p in pools]))


def _bare_discovery(store):
    d = BasePoolDiscovery.__new__(BasePoolDiscovery)
    d.graph = LivePoolGraph()
    d.aerodrome_factory = addr(0xAA)
    d.uniswap_v3_factory = ""
    d.token_allowlist = set()
    d.concurrency = 4
    d._aerodrome_cursor = 0
    d.uniswap_from_block = 0
    d._uniswap_cursor = 0
    d.log_chunk_blocks = 100
    d.store = store
    d._restored = True
    d._lock = asyncio.Lock()
    return d


def test_aerodrome_cursor_does_not_skip_transiently_failed_indexes(monkeypatch):
    """A transient RPC error must not advance the persisted cursor past that pool."""
    import arb_bot.pool_discovery as pd
    from arb_bot.pool_discovery import DiscoveryStats

    monkeypatch.setattr(pd.AsyncWeb3, "to_checksum_address", staticmethod(lambda x: x), raising=False)
    addrs = [addr(i) for i in range(1, 6)]
    fail_index = {2}

    class _Call:
        def __init__(self, i):
            self.i = i

        async def call(self):
            if self.i in fail_index:
                raise TimeoutError("transient rpc")
            return addrs[self.i]

    class _Length:
        async def call(self):
            return len(addrs)

    class _Fns:
        def allPoolsLength(self):
            return _Length()

        def allPools(self, i):
            return _Call(i)

    class _Factory:
        functions = _Fns()

    class _W3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return _Factory()

    store = _RecordingStore()
    d = _bare_discovery(store)
    d.w3 = _W3()

    async def _has_code(_a):
        return True

    async def _verify(address):
        return PoolMetadata.create(
            address=address, token0=addr(1), token1=addr(2),
            venue="aerodrome", pool_type="xyk", stable=False,
        )

    d._has_code = _has_code
    d._verify_aerodrome = _verify

    asyncio.run(d.discover_aerodrome(DiscoveryStats()))
    # Cursor holds at the failed index instead of jumping to the end.
    assert d._aerodrome_cursor == 2
    assert store.saved[-1][1] == 2

    # Once the transient failure clears, the retry picks the skipped pool up.
    fail_index.clear()
    asyncio.run(d.discover_aerodrome(DiscoveryStats()))
    assert d._aerodrome_cursor == len(addrs)
    registered = {p["address"] for p in d.graph.snapshot()["pools"]}
    assert addrs[2] in registered
    assert len(registered) == len(addrs)
