"""Curation lifecycle: register/select/delete and restart persistence."""
import asyncio

from arb_bot.event_graph import LivePoolGraph, PoolMetadata
from arb_bot.models import PoolRegistration


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


class _InMemoryCuratedStore:
    """Stands in for PostgresCuratedPoolStore with identical semantics."""

    def __init__(self):
        self.rows: dict[str, PoolMetadata] = {}

    async def load(self):
        return list(self.rows.values())

    async def add(self, pool):
        self.rows[pool.address] = pool

    async def remove(self, address):
        return self.rows.pop(PoolMetadata.normalize_address(address), None) is not None

    async def replace(self, pools):
        materialised = list(pools)
        self.rows = {p.address: p for p in materialised}
        return len(materialised)


def _registration(n: int, **kw) -> PoolRegistration:
    body = {"address": addr(n), "token0": addr(100), "token1": addr(101),
            "venue": "aerodrome-slipstream", "pool_type": "concentrated", "tick_spacing": 100}
    body.update(kw)
    return PoolRegistration(**body)


def _metadata(reg: PoolRegistration) -> PoolMetadata:
    return PoolMetadata.create(
        address=reg.address, token0=reg.token0, token1=reg.token1,
        venue=reg.venue, pool_type=reg.pool_type, fee=reg.fee,
        stable=reg.stable, tick_spacing=reg.tick_spacing,
    )


def test_api_metadata_conversion_preserves_the_slipstream_routing_key():
    """The registration payload used to drop tick_spacing on the way into the graph."""
    assert _metadata(_registration(1)).tick_spacing == 100


def test_curated_selection_survives_a_restart():
    """With auto-discovery off, nothing else repopulates the graph after a restart."""
    store = _InMemoryCuratedStore()
    graph = LivePoolGraph()

    selection = [_metadata(_registration(n)) for n in (1, 2)]
    asyncio.run(store.replace(selection))
    graph.replace_all(selection)
    assert graph.addresses() == [addr(1), addr(2)]

    # Process restart: a fresh graph, rehydrated only from the durable store.
    restarted = LivePoolGraph()
    for pool in asyncio.run(store.load()):
        restarted.register(pool)
    assert restarted.addresses() == [addr(1), addr(2)]
    assert restarted.get(addr(1)).tick_spacing == 100


def test_selection_is_authoritative_rather_than_cumulative():
    store = _InMemoryCuratedStore()
    graph = LivePoolGraph()

    first = [_metadata(_registration(n)) for n in (1, 2, 3)]
    asyncio.run(store.replace(first))
    graph.replace_all(first)

    # A tighter floor keeps only one pool; the other two must actually disappear.
    second = [_metadata(_registration(2))]
    asyncio.run(store.replace(second))
    graph.replace_all(second)

    assert graph.addresses() == [addr(2)]
    assert [p.address for p in asyncio.run(store.load())] == [addr(2)]


def test_delete_removes_a_pool_from_both_the_graph_and_the_durable_store():
    store = _InMemoryCuratedStore()
    graph = LivePoolGraph()
    pool = _metadata(_registration(1))
    asyncio.run(store.add(pool))
    graph.register(pool)

    assert asyncio.run(store.remove(addr(1))) is True
    assert graph.unregister(addr(1)) is True
    assert graph.addresses() == []
    assert asyncio.run(store.load()) == []
    # Idempotent: a second delete reports "nothing there" rather than failing.
    assert asyncio.run(store.remove(addr(1))) is False


# --- the selection must hold against automatic discovery -----------------------------


def _discovered(n: int) -> PoolMetadata:
    return PoolMetadata.create(
        address=addr(n), token0=addr(100), token1=addr(101),
        venue="aerodrome", pool_type="xyk", stable=False,
    )


def test_a_selection_is_not_undone_by_the_next_discovery_refresh():
    """Discovery is enabled by default and re-registers every verified pool each pass."""
    graph = LivePoolGraph()
    for n in (50, 51, 52):
        graph.register(_discovered(n))

    graph.replace_all([_metadata(_registration(n)) for n in (1, 2)])
    assert graph.addresses() == [addr(1), addr(2)]

    for n in (50, 51, 52, 53):  # a refresh, including a newly discovered pool
        graph.register(_discovered(n))
    assert graph.addresses() == [addr(1), addr(2)]


def test_restoring_a_selection_at_startup_restricts_rather_than_merely_registering():
    """Registering the curated pools alone left discovery free to add everything back."""
    store = _InMemoryCuratedStore()
    asyncio.run(store.replace([_metadata(_registration(n)) for n in (1, 2)]))

    graph = LivePoolGraph()
    restored = asyncio.run(store.load())
    graph.replace_all(restored)          # what startup does
    for n in (50, 51):                   # the discovery loop, moments later
        graph.register(_discovered(n))

    assert graph.addresses() == [addr(1), addr(2)]
    assert graph.snapshot()["restricted_to"] == 2


def test_no_curated_selection_leaves_discovery_unrestricted():
    """An empty curated table means "not curating", not "route nothing"."""
    store = _InMemoryCuratedStore()
    graph = LivePoolGraph()

    restored = asyncio.run(store.load())
    if restored:
        graph.replace_all(restored)
    else:
        graph.restrict_to(None)

    for n in (50, 51):
        graph.register(_discovered(n))
    assert graph.addresses() == [addr(50), addr(51)]
    assert graph.snapshot()["restricted_to"] is None
