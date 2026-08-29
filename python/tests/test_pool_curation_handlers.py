"""Curation through the actual FastAPI handlers, including concurrency and restore retry.

The rest of the curation tests drive LivePoolGraph and the store directly, which cannot
see anything that goes wrong *between* the two -- the interleaving of a database await and
a graph mutation is exactly where the store and the graph drift apart. These call the
handler coroutines.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from arb_bot import api
from arb_bot.event_graph import LivePoolGraph, PoolMetadata
from arb_bot.models import PoolRegistration


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def _registration(n: int) -> PoolRegistration:
    return PoolRegistration(
        address=addr(n), token0=addr(100), token1=addr(101),
        venue="aerodrome-slipstream", pool_type="concentrated", tick_spacing=100,
    )


def _discovered(n: int) -> PoolMetadata:
    return PoolMetadata.create(
        address=addr(n), token0=addr(100), token1=addr(101),
        venue="aerodrome", pool_type="xyk", stable=False,
    )


class _SlowStore:
    """Curated store whose writes yield, so handlers can interleave the way they do live."""

    def __init__(self, *, load_error: Exception | None = None, write_delay: float = 0.0):
        self.rows: dict[str, PoolMetadata] = {}
        self.load_error = load_error
        self.write_delay = write_delay
        self.load_calls = 0

    async def load(self):
        self.load_calls += 1
        if self.load_error is not None:
            raise self.load_error
        return list(self.rows.values())

    async def _pause(self):
        await asyncio.sleep(self.write_delay)

    async def add(self, pool):
        await self._pause()
        self.rows[pool.address] = pool

    async def remove(self, address):
        await self._pause()
        return self.rows.pop(PoolMetadata.normalize_address(address), None) is not None

    async def replace(self, pools):
        materialised = list(pools)
        await self._pause()
        self.rows = {p.address: p for p in materialised}
        return len(materialised)


@pytest.fixture(autouse=True)
def _fresh_locks(monkeypatch):
    """Rebind the module's locks for this test's event loop.

    asyncio.Lock binds to the first loop that contends on it, and every test here runs its
    own asyncio.run(). Rebuilt via type(...) rather than asyncio.Lock() on purpose: an
    implementation that dropped the lock would get its own no-op type back, so these tests
    still fail instead of quietly supplying the serialisation they are meant to verify.
    """
    for name in ("_curation_lock", "_schema_lock"):
        current = getattr(api, name)
        monkeypatch.setattr(api, name, type(current)())


class _FakeRegistry:
    """Stands in for PostgresPoolRegistry: everything discovery has verified so far."""

    def __init__(self, pools=(), error: Exception | None = None):
        self.pools = list(pools)
        self.error = error
        self.calls = 0

    async def load_pools(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.pools)


class _AdmitAll:
    @staticmethod
    def admits(_pool):
        return True


@pytest.fixture
def wired(monkeypatch):
    """A fresh graph + store bound into the api module, with schema pretend-ready."""
    graph = LivePoolGraph()
    store = _SlowStore()
    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pools", store)
    monkeypatch.setattr(api, "pool_registry", _FakeRegistry())
    monkeypatch.setattr(api, "pool_discovery", _AdmitAll())
    monkeypatch.setitem(api.schema_status, "ready", True)
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": True, "last_error": None,
    })
    # Deliberately NOT substituting api._curation_lock: replacing it with a fresh Lock
    # would make these tests pass against an implementation that has no lock at all.
    return graph, store


# --- concurrent mutations must not diverge the store from the graph ------------------


def _stall_replace(store, gate: asyncio.Event):
    """Make store.replace() commit, then park until `gate` is set.

    Reproduces the real window deterministically: every handler awaits the database and
    only then mutates the graph, so the interesting interleaving is another handler
    running to completion inside that gap.
    """

    async def replace(pools):
        materialised = list(pools)
        store.rows = {p.address: p for p in materialised}  # the commit
        await gate.wait()                                   # ... before the graph mutation
        return len(materialised)

    store.replace = replace


def test_register_during_an_inflight_select_does_not_diverge_store_and_graph(wired):
    """select(B) commits, register(C) commits and admits, select(B) then replaces.

    Postgres ended up holding {B, C} while the graph held {B}, so C reappeared at the next
    restart -- a pool the operator never selected, routing live.
    """
    graph, store = wired

    async def scenario():
        await api.select_pools([_registration(1)])  # a selection must be in force
        gate = asyncio.Event()
        _stall_replace(store, gate)

        select = asyncio.create_task(api.select_pools([_registration(2)]))
        await asyncio.sleep(0)   # select commits, then parks before its graph mutation
        register = asyncio.create_task(api.register_pool(_registration(3)))
        await asyncio.sleep(0)   # register commits and admits C into the graph
        gate.set()               # select resumes and replaces the graph
        await asyncio.gather(select, register)

    asyncio.run(scenario())

    assert set(store.rows) == set(graph.addresses()), (
        f"durable selection {sorted(store.rows)} and graph {graph.addresses()} diverged"
    )


def test_delete_during_an_inflight_select_does_not_diverge_store_and_graph(wired):
    graph, store = wired

    async def scenario():
        await api.select_pools([_registration(1), _registration(2)])
        gate = asyncio.Event()
        _stall_replace(store, gate)

        select = asyncio.create_task(
            api.select_pools([_registration(1), _registration(2)])
        )
        await asyncio.sleep(0)
        delete = asyncio.create_task(api.unregister_pool(addr(2)))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.gather(select, delete)

    asyncio.run(scenario())
    assert set(store.rows) == set(graph.addresses()), (
        f"durable selection {sorted(store.rows)} and graph {graph.addresses()} diverged"
    )


def test_many_interleaved_mutations_keep_the_two_in_step(wired):
    graph, store = wired
    store.write_delay = 0.001

    async def scenario():
        await api.select_pools([_registration(n) for n in (1, 2, 3)])
        await asyncio.gather(
            api.select_pools([_registration(n) for n in (1, 2)]),
            api.register_pool(_registration(4)),
            api.register_pool(_registration(5)),
            api.unregister_pool(addr(1)),
            return_exceptions=True,
        )

    asyncio.run(scenario())
    assert set(store.rows) == set(graph.addresses())


# --- a restore that fails after the schema is ready must keep retrying ----------------


def test_restore_is_retried_when_only_the_curated_load_failed(monkeypatch):
    """Folding the restore into the schema branch made it unreachable once schema was ready."""
    graph = LivePoolGraph()
    store = _SlowStore(load_error=TimeoutError("database went away"))
    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pools", store)
    monkeypatch.setattr(api, "pool_registry", _FakeRegistry())
    monkeypatch.setattr(api, "pool_discovery", _AdmitAll())
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": False, "last_error": None,
    })
    monkeypatch.setitem(api.schema_status, "ready", True)

    # Schema is already ready; the restore is what is failing.
    assert asyncio.run(api._ensure_schema()) is False
    assert store.load_calls == 1
    assert graph.snapshot()["restricted_to"] == 0, "must fail closed, not run unrestricted"

    assert asyncio.run(api._ensure_schema()) is False
    assert store.load_calls == 2, "every call must retry while the selection is unknown"

    # Database recovers.
    store.load_error = None
    store.rows = {addr(1): _discovered(1)}
    assert asyncio.run(api._ensure_schema()) is True
    assert graph.addresses() == [addr(1)]
    assert api.curated_pool_status["loaded"] is True

    # And it stops re-reading once it has landed.
    calls = store.load_calls
    assert asyncio.run(api._ensure_schema()) is True
    assert store.load_calls == calls


def test_ready_reports_503_while_the_selection_is_unknown(monkeypatch):
    """A process whose schema is fine but whose selection is not is routing nothing."""
    graph = LivePoolGraph()
    store = _SlowStore(load_error=TimeoutError("database went away"))
    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pools", store)
    monkeypatch.setattr(api, "pool_registry", _FakeRegistry())
    monkeypatch.setattr(api, "pool_discovery", _AdmitAll())
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": False, "last_error": None,
    })
    monkeypatch.setitem(api.schema_status, "ready", True)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(api.ready())
    assert raised.value.status_code == 503
    assert "curated pool selection unavailable" in raised.value.detail


def test_an_unreadable_selection_holds_the_graph_empty_against_discovery(monkeypatch):
    graph = LivePoolGraph()
    store = _SlowStore(load_error=TimeoutError("database went away"))
    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pools", store)
    monkeypatch.setattr(api, "pool_registry", _FakeRegistry())
    monkeypatch.setattr(api, "pool_discovery", _AdmitAll())
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": False, "last_error": None,
    })
    monkeypatch.setitem(api.schema_status, "ready", True)

    asyncio.run(api._restore_curated_pools())
    for n in (50, 51):
        graph.register(_discovered(n))
    assert graph.addresses() == []


# --- graph-only delete must not touch the store --------------------------------------


def test_delete_without_a_selection_never_reads_or_writes_the_store(wired):
    """It used to 503 during a database outage on a removal that is graph-only anyway."""
    graph, store = wired
    graph.register(_discovered(1))

    async def _explode(_address):
        raise TimeoutError("database went away")

    store.remove = _explode

    body = asyncio.run(api.unregister_pool(addr(1)))
    assert body["removed"] is True
    assert body["removed_from_store"] is False
    assert body["permanent"] is False
    assert graph.addresses() == []


def test_delete_with_a_selection_is_permanent_and_writes_through(wired):
    graph, store = wired
    asyncio.run(api.select_pools([_registration(1), _registration(2)]))

    body = asyncio.run(api.unregister_pool(addr(2)))
    assert body["permanent"] is True
    assert body["removed_from_store"] is True
    assert set(store.rows) == {addr(1)}
    graph.register(_discovered(2))  # a discovery refresh
    assert graph.addresses() == [addr(1)]


def test_register_without_a_selection_is_graph_only(wired):
    graph, store = wired
    body = asyncio.run(api.register_pool(_registration(1)))
    assert body["curated"] is False
    assert store.rows == {}, "a lone registration must not seed a one-pool selection"
    assert graph.addresses() == [addr(1)]


def test_register_during_a_fail_closed_hold_does_not_seed_a_one_pool_selection(monkeypatch):
    """The hold is not a selection, even though it installs a restriction.

    _hold_graph_empty() fails closed with restrict_to([]), so `restriction() is not None`
    was true while the store was unreadable. One /pools/register then took the curated
    branch and persisted a row into an empty `curated_pools`; the restore that ran once the
    database came back read that single row as the entire selection and collapsed the
    routable graph onto it -- permanently, and triggered by nothing worse than Postgres
    being slow to accept connections at cold start.
    """
    graph = LivePoolGraph()
    store = _SlowStore(load_error=TimeoutError("database still starting"))
    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pools", store)
    monkeypatch.setattr(api, "pool_registry", _FakeRegistry([_discovered(50), _discovered(51)]))
    monkeypatch.setattr(api, "pool_discovery", _AdmitAll())
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": False, "last_error": None,
    })
    monkeypatch.setitem(api.schema_status, "ready", True)

    async def scenario():
        await api._restore_curated_pools()          # fails -> graph held empty
        body = await api.register_pool(_registration(1))
        assert body["curated"] is False, "a hold is not a selection in force"
        assert store.rows == {}, "the hold must not be written through to the store"

        store.load_error = None                     # database comes back
        await api._restore_curated_pools()

    asyncio.run(scenario())

    # An empty curated table still means "no selection", so discovery owns the graph again.
    assert set(graph.addresses()) == {addr(50), addr(51)}, (
        "the graph collapsed onto the pool registered during the hold"
    )
    assert graph.restriction() is None


# --- lifting curation must give the graph back, not just unlock it -------------------


def test_lifting_curation_restores_the_pools_the_filter_turned_away(wired, monkeypatch):
    """restrict_to(None) alone leaves the graph holding only the ex-selection.

    Pools discovery registered while the filter was up were dropped at the door -- the
    graph never held them -- and discovery's cursors have advanced past them, so nothing
    re-registers them until a restart.
    """
    graph, _store = wired
    registry = _FakeRegistry([_discovered(n) for n in (50, 51, 52)])
    monkeypatch.setattr(api, "pool_registry", registry)

    asyncio.run(api.select_pools([_registration(1), _registration(2)]))
    for n in (50, 51, 52):          # a discovery refresh, all filtered out
        graph.register(_discovered(n))
    assert graph.addresses() == [addr(1), addr(2)]

    body = asyncio.run(api.select_pools([]))

    assert body["restricted"] is False
    assert graph.addresses() == [addr(50), addr(51), addr(52)], (
        "the graph must be rebuilt from verified_pools, not merely unfiltered"
    )
    assert graph.snapshot()["restricted_to"] is None


def test_lifting_curation_is_refused_when_the_registry_cannot_be_read(wired, monkeypatch):
    """Better to keep the current selection than to unfilter onto a near-empty graph."""
    graph, store = wired
    asyncio.run(api.select_pools([_registration(1), _registration(2)]))
    monkeypatch.setattr(
        api, "pool_registry", _FakeRegistry(error=TimeoutError("database went away"))
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(api.select_pools([]))
    assert raised.value.status_code == 503
    assert "could not be rebuilt" in raised.value.detail

    # Nothing was committed and nothing was dropped.
    assert graph.addresses() == [addr(1), addr(2)]
    assert set(store.rows) == {addr(1), addr(2)}


def test_recovering_with_an_empty_curated_table_rebuilds_from_discovery(monkeypatch):
    """The fail-closed empty restriction filtered out discovery's one-shot restore()."""
    graph = LivePoolGraph()
    store = _SlowStore(load_error=TimeoutError("database went away"))
    registry = _FakeRegistry([_discovered(n) for n in (50, 51)])
    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pools", store)
    monkeypatch.setattr(api, "pool_registry", registry)
    monkeypatch.setattr(api, "pool_discovery", _AdmitAll())
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": False, "last_error": None,
    })
    monkeypatch.setitem(api.schema_status, "ready", True)

    asyncio.run(api._ensure_schema())          # restore fails -> graph held empty
    graph.register(_discovered(50))            # discovery's restore(), all filtered out
    assert graph.addresses() == []

    store.load_error = None                    # database recovers, no curated rows
    assert asyncio.run(api._ensure_schema()) is True
    assert graph.addresses() == [addr(50), addr(51)], (
        "discovery restore() is idempotent and will not run again; the graph must be "
        "rebuilt from verified_pools"
    )
    assert graph.snapshot()["restricted_to"] is None


def test_the_token_allowlist_is_applied_when_rebuilding(wired, monkeypatch):
    """Rebuilding must use the same filter restore() does, not a looser one."""
    graph, _store = wired
    monkeypatch.setattr(
        api, "pool_registry", _FakeRegistry([_discovered(n) for n in (50, 51)])
    )

    class _AdmitOne:
        @staticmethod
        def admits(pool):
            return pool.address == addr(50)

    monkeypatch.setattr(api, "pool_discovery", _AdmitOne())
    asyncio.run(api.select_pools([_registration(1)]))
    asyncio.run(api.select_pools([]))
    assert graph.addresses() == [addr(50)]


def test_the_suite_is_not_pointed_at_a_reachable_database():
    """Guard: handlers under test open real connections from settings.database_url.

    With the compose Postgres running -- the normal developer state -- the suite wrote
    real rows into it and intermittently blocked on it. conftest points the URL at a
    closed port; this fails if that ever regresses.
    """
    import socket
    from urllib.parse import urlsplit

    from arb_bot.config import settings

    parsed = urlsplit(settings.database_url.replace("+asyncpg", ""))
    assert parsed.port not in (None, 5432), (
        f"tests must not target a live database port: {settings.database_url}"
    )
    # The port assertion above is the portable half. The reachability probe is a bonus
    # check and is skipped where the sandbox forbids opening sockets at all, rather than
    # failing the suite for an unrelated reason.
    try:
        probe = socket.socket()
    except (OSError, PermissionError):  # pragma: no cover - sandbox-dependent
        return
    probe.settimeout(0.5)
    try:
        connected = probe.connect_ex((parsed.hostname or "127.0.0.1", parsed.port)) == 0
    except OSError:  # pragma: no cover - sandbox-dependent
        return
    finally:
        probe.close()
    assert not connected, f"the test database URL is reachable: {settings.database_url}"


# --- discovery's commit must not slip through a restriction being lifted -------------


def _aerodrome_discovery(graph, addresses, *, save_batch, commit_lock):
    """A real BasePoolDiscovery driving discover_aerodrome() over `addresses`."""
    import arb_bot.pool_discovery as pd

    class _Ret:
        def __init__(self, value):
            self.value = value

        async def call(self):
            return self.value

    class _FactoryFns:
        def allPoolsLength(self):
            return _Ret(len(addresses))

        def allPools(self, i):
            return _Ret(addresses[i])

    class _PoolFns:
        def token0(self):
            return _Ret(addr(100))

        def token1(self):
            return _Ret(addr(101))

        def stable(self):
            return _Ret(False)

    class _Contract:
        def __init__(self, fns):
            self.functions = fns

    class _Node:
        class eth:
            @staticmethod
            def contract(address=None, abi=None):
                return _Contract(
                    _FactoryFns() if abi is pd.AERODROME_FACTORY_ABI else _PoolFns()
                )

    discovery = pd.BasePoolDiscovery.__new__(pd.BasePoolDiscovery)
    discovery.graph = graph
    discovery.aerodrome_factory = addr(0xAA)
    discovery.uniswap_v3_factory = ""
    discovery.slipstream_factory = ""
    discovery.token_allowlist = set()
    discovery.concurrency = 4
    discovery._aerodrome_cursor = 0
    discovery.store = SimpleNamespace(save_batch=save_batch)
    discovery._restored = True
    discovery.commit_lock = commit_lock
    discovery.w3 = _Node()
    discovery.call_w3 = discovery.w3

    async def _has_code(_a):
        return True

    discovery._has_code = _has_code
    return discovery


def test_a_pool_discovered_while_curation_is_lifted_is_not_lost(monkeypatch):
    """The rebuild's registry snapshot predates discovery's commit.

    1. lift takes its verified_pools snapshot
    2. discovery persists P and advances its cursor past it
    3. discovery registers P -- filtered by the restriction on its way out
    4. lift swaps in the older snapshot, unrestricted
    5. P is in verified_pools, absent from the graph, and behind the cursor: gone until
       a restart.
    """
    import arb_bot.pool_discovery as pd
    from arb_bot.pool_discovery import DiscoveryStats

    monkeypatch.setattr(pd.AsyncWeb3, "to_checksum_address", staticmethod(lambda x: x), raising=False)

    graph = LivePoolGraph()
    store = _SlowStore()
    persisted = {addr(50): _discovered(50)}          # what the snapshot will contain
    snapshot_taken = asyncio.Event()
    release_snapshot = asyncio.Event()

    class _GatedRegistry:
        async def load_pools(self):
            # Materialise *before* parking: the whole point is that the lift proceeds from
            # a view of verified_pools taken before discovery's commit.
            snapshot = list(persisted.values())
            snapshot_taken.set()
            await release_snapshot.wait()
            return snapshot

    async def save_batch(*, source, factory, next_cursor, pools, verified_block=None):
        for pool in pools:                            # discovery's durable write
            persisted[pool.address] = pool

    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pools", store)
    monkeypatch.setattr(api, "pool_registry", _GatedRegistry())
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": True, "last_error": None,
    })
    monkeypatch.setitem(api.schema_status, "ready", True)

    discovery = _aerodrome_discovery(
        graph, [addr(53)], save_batch=save_batch, commit_lock=api._curation_lock
    )
    monkeypatch.setattr(api, "pool_discovery", discovery)

    async def scenario():
        await api.select_pools([_registration(1)])   # a selection is in force
        lift = asyncio.create_task(api.select_pools([]))
        await snapshot_taken.wait()                  # step 1
        scan = asyncio.create_task(discovery.discover_aerodrome(DiscoveryStats()))
        # Let the scan run as far as it can get: to completion if nothing serialises its
        # commit (steps 2-3), or to a stop on the curation lock if something does. A fixed
        # number of yields rather than `await scan`, which would deadlock on the latter.
        for _ in range(100):
            if scan.done():
                break
            await asyncio.sleep(0)
        release_snapshot.set()                       # step 4
        await asyncio.gather(lift, scan)

    asyncio.run(scenario())

    assert addr(53) in persisted, "precondition: discovery persisted the new pool"
    assert discovery._aerodrome_cursor == 1, "precondition: its cursor advanced past it"
    assert addr(53) in graph.addresses(), (
        f"pool discovered during the lift is in verified_pools and behind the cursor but "
        f"missing from the graph: {graph.addresses()}"
    )
    assert graph.snapshot()["restricted_to"] is None


def test_discovery_without_a_commit_lock_still_works():
    """The lock is supplied by the service; the class must not require one."""
    import arb_bot.pool_discovery as pd
    from arb_bot.pool_discovery import DiscoveryStats

    graph = LivePoolGraph()
    saved = []

    async def save_batch(*, source, factory, next_cursor, pools, verified_block=None):
        saved.extend(p.address for p in pools)

    discovery = _aerodrome_discovery(
        graph, [addr(60), addr(61)], save_batch=save_batch, commit_lock=None
    )
    original = pd.AsyncWeb3.to_checksum_address
    pd.AsyncWeb3.to_checksum_address = staticmethod(lambda x: x)
    try:
        asyncio.run(discovery.discover_aerodrome(DiscoveryStats()))
    finally:
        pd.AsyncWeb3.to_checksum_address = original
    assert sorted(saved) == [addr(60), addr(61)]
    assert graph.addresses() == [addr(60), addr(61)]


# --- startup must bind the socket before it reads 28.6k rows -------------------------


def test_startup_does_not_block_uvicorn_on_the_curated_restore(monkeypatch):
    """The schema + restore phase moved off the startup path, and the graph fails closed.

    On the default path (an empty `curated_pools`) the restore reads every row of
    `verified_pools` and rebuilds the graph from it. Awaiting that from startup() meant
    uvicorn could not bind until it finished -- the exact thing _discovery_loop and the
    backfill script both cite as unacceptable. Deferring it is only safe because the graph
    is held empty first, so nothing can route against an unknown selection in the gap.
    """
    graph = LivePoolGraph()
    graph.register(_discovered(1))          # pretend a previous state left pools behind
    monkeypatch.setattr(api, "pool_graph", graph)
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": True, "last_error": None,
    })
    monkeypatch.setattr(api.settings, "auto_pool_discovery_enabled", False)

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def _slow_ensure_schema():
            entered.set()
            await release.wait()            # a restore that has not landed yet
            return True

        monkeypatch.setattr(api, "_ensure_schema", _slow_ensure_schema)

        await asyncio.wait_for(api.startup(), timeout=1.0)   # must not wait on the restore

        # Held closed the instant startup returns, before anything can serve a route.
        assert graph.addresses() == []
        assert graph.restriction() == set()
        assert api.curated_pool_status["loaded"] is False

        await asyncio.sleep(0)
        assert entered.is_set(), "the restore must still run, just not on the bind path"
        release.set()
        for task in list(api._background_tasks):
            await task

    asyncio.run(scenario())


def test_an_unusable_operator_token_is_reported_at_boot(monkeypatch, caplog):
    """The operator who set the token is not the one who will hit the 503 on it.

    Without this the defect surfaces only on the first curation request, to whoever made
    it, as a refusal they cannot act on.
    """
    import logging

    monkeypatch.setattr(api, "pool_graph", LivePoolGraph())
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": True, "last_error": None,
    })
    monkeypatch.setattr(api.settings, "auto_pool_discovery_enabled", False)
    monkeypatch.setattr(api.settings, "operator_api_token", "tökén")

    async def _ok():
        return True

    monkeypatch.setattr(api, "_ensure_schema", _ok)

    async def scenario():
        with caplog.at_level(logging.ERROR):
            await api.startup()
        for task in list(api._background_tasks):
            await task

    asyncio.run(scenario())

    assert "OPERATOR_API_TOKEN" in caplog.text
    assert "non-ASCII" in caplog.text
    assert "tökén" not in caplog.text, "the log must not echo the secret itself"


def test_a_usable_operator_token_boots_quietly(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(api, "pool_graph", LivePoolGraph())
    monkeypatch.setattr(api, "curated_pool_status", {
        "restored": 0, "restricted": False, "loaded": True, "last_error": None,
    })
    monkeypatch.setattr(api.settings, "auto_pool_discovery_enabled", False)
    monkeypatch.setattr(api.settings, "operator_api_token", "s3cret-operator-token")

    async def _ok():
        return True

    monkeypatch.setattr(api, "_ensure_schema", _ok)

    async def scenario():
        with caplog.at_level(logging.ERROR):
            await api.startup()
        for task in list(api._background_tasks):
            await task

    asyncio.run(scenario())
    assert "OPERATOR_API_TOKEN" not in caplog.text

