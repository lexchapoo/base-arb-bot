from arb_bot.event_graph import LivePoolGraph, PoolMetadata


def test_affected_subgraph_only_returns_neighboring_pools():
    graph = LivePoolGraph()
    token_a = "0x0000000000000000000000000000000000000001"
    token_b = "0x0000000000000000000000000000000000000002"
    token_c = "0x0000000000000000000000000000000000000003"
    token_d = "0x0000000000000000000000000000000000000004"
    pool_ab = "0x0000000000000000000000000000000000000011"
    pool_bc = "0x0000000000000000000000000000000000000012"
    pool_cd = "0x0000000000000000000000000000000000000013"

    graph.register(PoolMetadata.create(address=pool_ab, token0=token_a, token1=token_b, venue="test", pool_type="xyk"))
    graph.register(PoolMetadata.create(address=pool_bc, token0=token_b, token1=token_c, venue="test", pool_type="xyk"))
    graph.register(PoolMetadata.create(address=pool_cd, token0=token_c, token1=token_d, venue="test", pool_type="xyk"))

    affected = graph.affected_subgraph(pool_ab)
    assert affected["known_pool"] is True
    assert affected["affected_pools"] == sorted([pool_ab, pool_bc])
    assert pool_cd not in affected["affected_pools"]


def test_unknown_pool_does_not_trigger_global_rescan():
    graph = LivePoolGraph()
    unknown = "0x0000000000000000000000000000000000000099"
    affected = graph.affected_subgraph(unknown)
    assert affected == {
        "known_pool": False,
        "changed_pool": unknown,
        "affected_tokens": [],
        "affected_pools": [],
    }


# --- curation needs the graph to be a set, not an accumulator -------------------------


def _pool(n: int, t0: int, t1: int, **kw) -> PoolMetadata:
    return PoolMetadata.create(
        address="0x" + f"{n:040x}", token0="0x" + f"{t0:040x}", token1="0x" + f"{t1:040x}",
        venue=kw.pop("venue", "test"), pool_type=kw.pop("pool_type", "xyk"), **kw
    )


def test_unregister_removes_the_pool_and_its_token_adjacency():
    graph = LivePoolGraph()
    graph.register(_pool(1, 10, 11))
    graph.register(_pool(2, 10, 12))

    assert graph.unregister("0x" + f"{1:040x}") is True
    assert graph.contains("0x" + f"{1:040x}") is False
    # The shared token keeps only the surviving pool; the exclusive one is gone entirely.
    assert [p.address for p in graph.pools_for_token("0x" + f"{10:040x}")] == ["0x" + f"{2:040x}"]
    assert graph.pools_for_token("0x" + f"{11:040x}") == []


def test_unregister_is_idempotent_and_tolerates_garbage():
    graph = LivePoolGraph()
    assert graph.unregister("0x" + f"{1:040x}") is False
    assert graph.unregister("not-an-address") is False


def test_replace_all_drops_pools_absent_from_the_new_selection():
    """Registration alone is additive, so a re-curation could never shrink the graph."""
    graph = LivePoolGraph()
    graph.register(_pool(1, 10, 11))
    graph.register(_pool(2, 10, 12))

    count = graph.replace_all([_pool(2, 10, 12), _pool(3, 12, 13)])

    assert count == 2
    assert graph.addresses() == ["0x" + f"{2:040x}", "0x" + f"{3:040x}"]
    assert graph.pools_for_token("0x" + f"{11:040x}") == []
    assert [p.address for p in graph.pools_for_token("0x" + f"{12:040x}")] == [
        "0x" + f"{2:040x}", "0x" + f"{3:040x}"
    ]


def test_snapshot_carries_tick_spacing_so_slipstream_pools_are_inspectable():
    graph = LivePoolGraph()
    graph.register(_pool(1, 10, 11, venue="aerodrome-slipstream", pool_type="concentrated", tick_spacing=100))
    assert graph.snapshot()["pools"][0]["tick_spacing"] == 100


# --- a curated selection must survive concurrent discovery ---------------------------


def test_restriction_filters_out_pools_discovery_tries_to_add_back():
    """Discovery is on by default and re-registers every verified pool each refresh."""
    graph = LivePoolGraph()
    graph.replace_all([_pool(1, 10, 11), _pool(2, 10, 12)])

    graph.register(_pool(3, 10, 13))  # a discovery refresh
    assert graph.addresses() == ["0x" + f"{1:040x}", "0x" + f"{2:040x}"]
    assert graph.pools_for_token("0x" + f"{13:040x}") == []


def test_restriction_admits_pools_it_names_even_if_they_arrive_later():
    """Curation can name a pool discovery has not registered into the graph yet."""
    graph = LivePoolGraph()
    graph.restrict_to(["0x" + f"{1:040x}", "0x" + f"{2:040x}"])
    assert graph.addresses() == []

    graph.register(_pool(1, 10, 11))
    graph.register(_pool(9, 10, 19))
    assert graph.addresses() == ["0x" + f"{1:040x}"]


def test_restrict_to_drops_pools_already_outside_the_selection():
    graph = LivePoolGraph()
    graph.register(_pool(1, 10, 11))
    graph.register(_pool(2, 10, 12))

    graph.restrict_to(["0x" + f"{2:040x}"])
    assert graph.addresses() == ["0x" + f"{2:040x}"]
    assert graph.pools_for_token("0x" + f"{11:040x}") == []


def test_admit_widens_the_restriction_so_the_pool_is_not_filtered_out():
    graph = LivePoolGraph()
    graph.replace_all([_pool(1, 10, 11)])

    graph.admit(_pool(2, 10, 12))
    assert graph.addresses() == ["0x" + f"{1:040x}", "0x" + f"{2:040x}"]
    # And it stays through the next discovery refresh.
    graph.register(_pool(2, 10, 12))
    graph.register(_pool(3, 10, 13))
    assert graph.addresses() == ["0x" + f"{1:040x}", "0x" + f"{2:040x}"]


def test_unregister_narrows_the_restriction_so_discovery_cannot_reinstate_it():
    graph = LivePoolGraph()
    graph.replace_all([_pool(1, 10, 11), _pool(2, 10, 12)])

    assert graph.unregister("0x" + f"{2:040x}") is True
    graph.register(_pool(2, 10, 12))  # a discovery refresh
    assert graph.addresses() == ["0x" + f"{1:040x}"]


def test_lifting_the_restriction_lets_discovery_own_the_graph_again():
    graph = LivePoolGraph()
    graph.replace_all([_pool(1, 10, 11)])
    assert graph.restriction() == {"0x" + f"{1:040x}"}

    graph.restrict_to(None)
    assert graph.restriction() is None
    graph.register(_pool(3, 10, 13))
    assert graph.addresses() == ["0x" + f"{1:040x}", "0x" + f"{3:040x}"]


def test_an_empty_restriction_holds_the_graph_empty_rather_than_unrestricted():
    """The fail-closed state when the curated set cannot be read from the database."""
    graph = LivePoolGraph()
    graph.register(_pool(1, 10, 11))

    graph.restrict_to([])
    assert graph.addresses() == []
    graph.register(_pool(2, 10, 12))
    assert graph.addresses() == []
    assert graph.snapshot()["restricted_to"] == 0


def test_register_reports_whether_the_pool_actually_entered_the_graph():
    """A restriction silently drops most registrations, so the caller must be able to tell.

    Discovery incremented its `registered` stat once per verified pool regardless. Under a
    400-pool selection against 28.6k verified pools that reported tens of thousands of
    registrations next to a pool_count of 400.
    """
    graph = LivePoolGraph()
    inside = PoolMetadata.create(
        address="0x" + f"{1:040x}", token0="0x" + f"{100:040x}", token1="0x" + f"{101:040x}",
        venue="aerodrome", pool_type="xyk", stable=False,
    )
    outside = PoolMetadata.create(
        address="0x" + f"{2:040x}", token0="0x" + f"{100:040x}", token1="0x" + f"{101:040x}",
        venue="aerodrome", pool_type="xyk", stable=False,
    )

    assert graph.register(inside) is True, "unrestricted registration enters the graph"

    graph.restrict_to([inside.address])
    assert graph.register(inside) is True, "re-registering a selected pool still lands"
    assert graph.register(outside) is False, "a pool outside the selection is turned away"
    assert graph.addresses() == [inside.address]

