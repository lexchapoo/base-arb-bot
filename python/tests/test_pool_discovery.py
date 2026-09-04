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

import pytest

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


# --- split-RPC discovery: the verification endpoint must be provably the same chain ----


class _FakeEth:
    def __init__(self, chain_id, block_number):
        self._chain_id = chain_id
        self._block_number = block_number

    @property
    async def chain_id(self):
        return self._chain_id

    @property
    async def block_number(self):
        return self._block_number


class _FakeNode:
    def __init__(self, chain_id=8453, block_number=1_000_000):
        self.eth = _FakeEth(chain_id, block_number)


def _split_rpc_discovery(*, call_chain_id=8453, call_block=1_000_000, log_block=1_000_000, max_lag=64):
    d = _bare_discovery(_RecordingStore())
    d.w3 = _FakeNode(8453, log_block)
    d.call_w3 = _FakeNode(call_chain_id, call_block)
    d.max_call_endpoint_lag_blocks = max_lag
    d._endpoints_verified = False
    return d


def test_endpoint_check_is_a_noop_when_logs_and_calls_share_one_endpoint():
    d = _bare_discovery(_RecordingStore())
    d.w3 = _FakeNode()
    d.call_w3 = d.w3
    assert asyncio.run(d.verify_endpoint_consistency()) == {}


def test_split_rpc_verification_endpoint_on_the_wrong_chain_is_rejected():
    """A wrong-chain call endpoint makes every pool look like it has no code."""
    d = _split_rpc_discovery(call_chain_id=1)
    try:
        asyncio.run(d.verify_endpoint_consistency())
    except RuntimeError as exc:
        assert "disagree on chain" in str(exc)
    else:
        raise AssertionError("wrong-chain verification endpoint must not be accepted")


def test_split_rpc_verification_endpoint_lagging_too_far_is_rejected():
    """A lagging call endpoint has not seen recently created pools yet."""
    d = _split_rpc_discovery(call_block=999_000, log_block=1_000_000, max_lag=64)
    try:
        asyncio.run(d.verify_endpoint_consistency())
    except RuntimeError as exc:
        assert "behind the log endpoint" in str(exc)
    else:
        raise AssertionError("a lagging verification endpoint must not be accepted")


def test_split_rpc_endpoints_within_lag_budget_are_accepted_and_cached():
    d = _split_rpc_discovery(call_block=999_990, log_block=1_000_000, max_lag=64)
    result = asyncio.run(d.verify_endpoint_consistency())
    assert result == {"log_head": 1_000_000, "call_head": 999_990, "call_endpoint_lag": 10}
    # Cached: a second call does not re-probe (and would not notice a now-broken endpoint
    # unless forced, which refresh() does every pass).
    d.call_w3 = _FakeNode(chain_id=1, block_number=0)
    assert asyncio.run(d.verify_endpoint_consistency()) == {}


def test_pool_with_no_code_holds_the_cursor_instead_of_being_skipped(monkeypatch):
    """An empty get_code on a factory-indexed pool is an endpoint fault, not an absent pool.

    Returning None there let the cursor advance past a pool that was never actually
    examined -- the exact failure a wrong-chain or lagging verification endpoint produces
    for *every* pool, invisibly.
    """
    import arb_bot.pool_discovery as pd
    from arb_bot.pool_discovery import AERODROME_FACTORY_ABI, DiscoveryStats

    monkeypatch.setattr(pd.AsyncWeb3, "to_checksum_address", staticmethod(lambda x: x), raising=False)
    addrs = [addr(i) for i in range(1, 5)]

    class _Ret:
        def __init__(self, value):
            self.value = value

        async def call(self):
            return self.value

    class _FactoryFns:
        def allPoolsLength(self):
            return _Ret(len(addrs))

        def allPools(self, i):
            return _Ret(addrs[i])

    class _PoolFns:
        def token0(self):
            return _Ret(addr(1))

        def token1(self):
            return _Ret(addr(2))

        def stable(self):
            return _Ret(False)

    class _Contract:
        def __init__(self, fns):
            self.functions = fns

    class _Node:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return _Contract(_FactoryFns() if abi is AERODROME_FACTORY_ABI else _PoolFns())

    store = _RecordingStore()
    d = _bare_discovery(store)
    d.w3 = _Node()
    d.call_w3 = d.w3
    codeless = {addrs[2]}

    async def _has_code(a):
        return a not in codeless

    d._has_code = _has_code

    stats = DiscoveryStats()
    asyncio.run(d.discover_aerodrome(stats))
    assert d._aerodrome_cursor == 2, "cursor must not advance past a pool that was never examined"
    assert stats.rejected_no_code == 1

    codeless.clear()
    stats = DiscoveryStats()
    asyncio.run(d.discover_aerodrome(stats))
    assert d._aerodrome_cursor == len(addrs)
    assert stats.rejected_no_code == 0
    assert addrs[2] in {p["address"] for p in d.graph.snapshot()["pools"]}


def test_code_missing_never_consumes_the_retry_budget(monkeypatch):
    """The bounded retry budget must not be able to skip an announced pool.

    Transient failures and deterministic metadata rejections are both survivable, so the
    budget exists to stop them pinning the cursor forever. Code absence is neither: it
    means the verification endpoint is wrong about the world, and skipping past it drops
    a real pool while every downstream signal reports a clean, complete scan.
    """
    import arb_bot.pool_discovery as pd
    from arb_bot.pool_discovery import AERODROME_FACTORY_ABI, DiscoveryStats

    monkeypatch.setattr(pd.AsyncWeb3, "to_checksum_address", staticmethod(lambda x: x), raising=False)
    addrs = [addr(i) for i in range(1, 5)]

    class _Ret:
        def __init__(self, value):
            self.value = value

        async def call(self):
            return self.value

    class _FactoryFns:
        def allPoolsLength(self):
            return _Ret(len(addrs))

        def allPools(self, i):
            return _Ret(addrs[i])

    class _PoolFns:
        def token0(self):
            return _Ret(addr(1))

        def token1(self):
            return _Ret(addr(2))

        def stable(self):
            return _Ret(False)

    class _Contract:
        def __init__(self, fns):
            self.functions = fns

    class _Node:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return _Contract(_FactoryFns() if abi is AERODROME_FACTORY_ABI else _PoolFns())

    store = _RecordingStore()
    d = _bare_discovery(store)
    d.w3 = _Node()
    d.call_w3 = d.w3
    d.max_cursor_retries = 2

    async def _has_code(a):
        return a != addrs[2]

    d._has_code = _has_code

    # Far more passes than the retry budget: the cursor must never move past index 2.
    for _ in range(6):
        asyncio.run(d.discover_aerodrome(DiscoveryStats()))
        assert d._aerodrome_cursor == 2
    assert "aerodrome-v2" in d.code_missing_blockers()

    # A deterministic metadata failure, by contrast, is still skipped after the budget.
    d._clear_code_missing("aerodrome-v2")

    async def _always_has_code(_a):
        return True

    async def _verify(address):
        if address == addrs[2]:
            raise TimeoutError("transient")
        return PoolMetadata.create(
            address=address, token0=addr(1), token1=addr(2),
            venue="aerodrome", pool_type="xyk", stable=False,
        )

    d._has_code = _always_has_code
    d._verify_aerodrome = _verify
    for _ in range(d.max_cursor_retries + 1):
        asyncio.run(d.discover_aerodrome(DiscoveryStats()))
    assert d._aerodrome_cursor > 2, "the bounded budget still applies to transient failures"


def _aerodrome_harness(monkeypatch, *, count=5):
    """A BasePoolDiscovery whose only real work is allPools(i) -> address.

    _has_code and _verify_aerodrome are substituted by the caller, so a test can say
    exactly which index fails and how, without standing up pool contracts.
    """
    import arb_bot.pool_discovery as pd

    monkeypatch.setattr(pd.AsyncWeb3, "to_checksum_address", staticmethod(lambda x: x), raising=False)
    addrs = [addr(i) for i in range(1, count + 1)]

    class _Ret:
        def __init__(self, value):
            self.value = value

        async def call(self):
            return self.value

    class _Fns:
        def allPoolsLength(self):
            return _Ret(len(addrs))

        def allPools(self, i):
            return _Ret(addrs[i])

    class _Factory:
        functions = _Fns()

    class _W3:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return _Factory()

    d = _bare_discovery(_RecordingStore())
    d.w3 = _W3()
    d.call_w3 = d.w3

    async def _has_code(_a):
        return True

    d._has_code = _has_code
    return d, addrs


def _ok_pool(address):
    return PoolMetadata.create(
        address=address, token0=addr(1), token1=addr(2),
        venue="aerodrome", pool_type="xyk", stable=False,
    )


def test_a_flapping_endpoint_cannot_spend_the_budget_and_skip_an_announced_pool(monkeypatch):
    """Codeless on some passes, timing out on others, must still never advance the cursor.

    The blocker was only consulted on passes that actually reported codeless. When the same
    overloaded endpoint timed out on that pool instead, the pass fell through to the generic
    failure branch, spent a retry against `max_cursor_retries`, and after enough such passes
    cleared the blocker and stepped the cursor past an announced pool that was never once
    verified -- while the scan reported success. Alternating is the realistic shape of a
    lagging node, not a contrived one.
    """
    from arb_bot.pool_discovery import DiscoveryStats, PoolCodeMissingError

    d, addrs = _aerodrome_harness(monkeypatch)
    d.max_cursor_retries = 2
    passes = {"n": 0}

    async def _verify(address):
        if address == addrs[2]:
            # Even passes: the endpoint says "no code". Odd passes: it times out.
            if passes["n"] % 2 == 0:
                raise PoolCodeMissingError(f"no deployed code at {address}")
            raise TimeoutError("transient rpc")
        return _ok_pool(address)

    d._verify_aerodrome = _verify

    for _ in range(8):  # well past max_cursor_retries in either direction
        asyncio.run(d.discover_aerodrome(DiscoveryStats()))
        passes["n"] += 1
        assert d._aerodrome_cursor == 2, (
            "the retry budget stepped the cursor past an unverified announced pool"
        )
    assert "aerodrome-v2" in d.code_missing_blockers()


def test_a_blocker_is_cleared_once_its_index_verifies_even_if_a_later_one_fails(monkeypatch):
    """The held index coming back verified resolves the blocker, whatever else failed.

    This scan examines every remaining index in one pass, so the held index can verify while
    some *later* index fails and takes over the cursor. The blocker used to survive that:
    neither clearing branch ran, so refresh() kept raising about an index that was long fine
    -- and because the raise happens after `result` is built, the caller discarded the
    cursors and never recorded a completed first scan.
    """
    from arb_bot.pool_discovery import DiscoveryStats, PoolCodeMissingError

    d, addrs = _aerodrome_harness(monkeypatch)
    d.max_cursor_retries = 5
    state = {"codeless": {addrs[2]}, "timeout": set()}

    async def _verify(address):
        if address in state["codeless"]:
            raise PoolCodeMissingError(f"no deployed code at {address}")
        if address in state["timeout"]:
            raise TimeoutError("transient rpc")
        return _ok_pool(address)

    d._verify_aerodrome = _verify

    asyncio.run(d.discover_aerodrome(DiscoveryStats()))
    assert d._aerodrome_cursor == 2
    assert "aerodrome-v2" in d.code_missing_blockers()

    # Index 2 recovers; index 3 now fails transiently and takes over the cursor.
    state["codeless"].clear()
    state["timeout"].add(addrs[3])
    asyncio.run(d.discover_aerodrome(DiscoveryStats()))

    assert d._aerodrome_cursor == 3
    assert d.code_missing_blockers() == {}, (
        "a blocker outlived the index that caused it; refresh() would keep raising"
    )


def test_a_blocker_is_not_cleared_when_its_index_merely_times_out_again(monkeypatch):
    """A timeout at the held index tells us nothing new, so it is not resolution."""
    from arb_bot.pool_discovery import DiscoveryStats, PoolCodeMissingError

    d, addrs = _aerodrome_harness(monkeypatch)
    d.max_cursor_retries = 2
    state = {"mode": "codeless"}

    async def _verify(address):
        if address == addrs[2]:
            if state["mode"] == "codeless":
                raise PoolCodeMissingError(f"no deployed code at {address}")
            raise TimeoutError("transient rpc")
        return _ok_pool(address)

    d._verify_aerodrome = _verify

    asyncio.run(d.discover_aerodrome(DiscoveryStats()))
    assert "aerodrome-v2" in d.code_missing_blockers()

    state["mode"] = "timeout"
    for _ in range(4):
        asyncio.run(d.discover_aerodrome(DiscoveryStats()))
    assert d._aerodrome_cursor == 2
    assert "aerodrome-v2" in d.code_missing_blockers()


def test_registered_counts_graph_entries_not_pools_turned_away_by_a_selection(monkeypatch):
    """Under a curated restriction most discovered pools never enter the routable graph.

    `registered` was incremented once per verified pool regardless, so /ready reported
    tens of thousands of registrations beside a pool_count of a few hundred.
    """
    from arb_bot.pool_discovery import DiscoveryStats

    d, addrs = _aerodrome_harness(monkeypatch)

    async def _verify(address):
        return _ok_pool(address)

    d._verify_aerodrome = _verify
    # A selection admitting exactly one of the five discovered pools.
    d.graph.restrict_to([addrs[1]])

    stats = DiscoveryStats()
    asyncio.run(d.discover_aerodrome(stats))

    assert d.graph.snapshot()["pool_count"] == 1
    assert stats.registered == 1, (
        f"counted {stats.registered} registrations into a graph holding 1 pool"
    )
    assert stats.aerodrome_seen == len(addrs), "every pool was still verified and persisted"


def _refresh_harness(*, latest=5_000):
    """A discovery whose three source scans are stubbed, so refresh() itself is under test."""
    from arb_bot.pool_discovery import BASE_CHAIN_ID

    class _Awaitable:
        def __init__(self, value):
            self.value = value

        def __await__(self):
            async def _v():
                return self.value
            return _v().__await__()

    class _Eth:
        @property
        def chain_id(self):
            return _Awaitable(BASE_CHAIN_ID)

        @property
        def block_number(self):
            return _Awaitable(latest)

    class _W3:
        eth = _Eth()

    d = _bare_discovery(_RecordingStore())
    d.w3 = _W3()
    d.call_w3 = d.w3
    d._slipstream_cursor = 0

    async def _restore():
        return {}

    async def _verify_endpoints(force=False):
        return {"log_head": latest, "call_head": latest, "call_endpoint_lag": 0}

    async def _noop(*_a, **_kw):
        return None

    d.restore = _restore
    d.verify_endpoint_consistency = _verify_endpoints
    d.discover_aerodrome = _noop
    d.discover_uniswap = _noop
    d.discover_slipstream = _noop
    return d


def test_a_blocked_source_still_reports_the_progress_of_the_others():
    """refresh() hands its assembled result over on the exception instead of dropping it.

    One held source used to erase the whole pass: the raise happens after `result` is
    built, so the caller saw only an exception and /ready reported a discovery that had
    never completed a scan -- while the two unblocked sources were advancing their cursors
    every pass. The cursors and stats are real measurements and must survive the raise.
    """
    from arb_bot.pool_discovery import PoolCodeMissingError

    d = _refresh_harness()
    d._aerodrome_cursor = 42
    d._slipstream_cursor = 4_900
    d._note_code_missing("uniswap-v3", 1_234, "blocks 1234-1334: 1 announced pool with no code")

    with pytest.raises(PoolCodeMissingError) as caught:
        asyncio.run(d.refresh())

    exc = caught.value
    assert exc.blockers == {
        "uniswap-v3": "blocks 1234-1334: 1 announced pool with no code"
    }
    assert exc.partial is not None, "the assembled result must survive the raise"
    assert exc.partial["aerodrome_cursor"] == 42
    assert exc.partial["slipstream_cursor"] == 4_900
    assert exc.partial["latest_block"] == 5_000


def test_the_low_level_code_missing_raise_carries_no_partial():
    """_require_code raises mid-scan, where there is no assembled result to attach."""
    from arb_bot.pool_discovery import PoolCodeMissingError

    exc = PoolCodeMissingError("no deployed code at 0xabc")
    assert exc.partial is None
    assert exc.blockers == {}


def test_discovery_status_publishes_a_blocked_passes_progress():
    """The api-side recorder applies the partial result without claiming a complete scan."""
    from arb_bot import api
    from arb_bot.pool_discovery import PoolCodeMissingError

    before = dict(api.pool_discovery_status)
    try:
        api.pool_discovery_status.update({
            "last_error": None, "first_scan_complete": False, "blocked_sources": {},
        })
        api._record_discovery_failure(PoolCodeMissingError(
            "discovery is blocked on announced pools with no code",
            partial={"latest_block": 5_000, "aerodrome_cursor": 42, "pool_count": 7},
            blockers={"uniswap-v3": "blocks 1234-1334"},
        ))

        assert api.pool_discovery_status["aerodrome_cursor"] == 42
        assert api.pool_discovery_status["pool_count"] == 7
        assert api.pool_discovery_status["blocked_sources"] == {"uniswap-v3": "blocks 1234-1334"}
        assert api.pool_discovery_status["first_scan_complete"] is False, (
            "a held source has not reached head"
        )
        assert "PoolCodeMissingError" in str(api.pool_discovery_status["last_error"])

        # A later clean pass clears the blocker list and latches the completed scan.
        api._record_discovery_success({"latest_block": 5_100, "aerodrome_cursor": 60})
        assert api.pool_discovery_status["blocked_sources"] == {}
        assert api.pool_discovery_status["first_scan_complete"] is True
        assert api.pool_discovery_status["last_error"] is None
    finally:
        api.pool_discovery_status.clear()
        api.pool_discovery_status.update(before)


def test_an_ordinary_discovery_failure_publishes_no_partial():
    """A scan that died before assembling a result must not invent progress."""
    from arb_bot import api

    before = dict(api.pool_discovery_status)
    try:
        api.pool_discovery_status.update({"blocked_sources": {}, "first_scan_complete": False})
        api._record_discovery_failure(TimeoutError("rpc went away"))
        assert api.pool_discovery_status["blocked_sources"] == {}
        assert api.pool_discovery_status["first_scan_complete"] is False
        assert "TimeoutError" in str(api.pool_discovery_status["last_error"])
    finally:
        api.pool_discovery_status.clear()
        api.pool_discovery_status.update(before)


def test_refresh_raises_while_a_code_missing_blocker_is_outstanding():
    """The cursors are held, so this is not a partial success to log once and forget."""
    from arb_bot.pool_discovery import PoolCodeMissingError

    d = _bare_discovery(_RecordingStore())
    d._note_code_missing("uniswap-v3", 100, "blocks 100-200: 1 announced pool with no code")
    assert d.code_missing_blockers() == {
        "uniswap-v3": "blocks 100-200: 1 announced pool with no code"
    }
    assert d._code_missing_cursor("uniswap-v3") == 100

    d._clear_code_missing("uniswap-v3")
    assert d.code_missing_blockers() == {}
    assert issubclass(PoolCodeMissingError, RuntimeError)
    assert not issubclass(PoolCodeMissingError, ValueError), (
        "must not be caught by the deterministic-rejection handler that lets the cursor pass"
    )
