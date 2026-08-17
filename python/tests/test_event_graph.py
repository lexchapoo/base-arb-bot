import json

import pytest

from arb_bot.event_graph import LivePoolGraph, PoolMetadata, load_configured_pools


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


def test_load_configured_pools_requires_exact_watchlist_match():
    pool = {
        "address": "0x0000000000000000000000000000000000000011",
        "token0": "0x0000000000000000000000000000000000000001",
        "token1": "0x0000000000000000000000000000000000000002",
        "venue": "uniswap-v3",
        "pool_type": "concentrated",
        "fee": 500,
    }
    loaded = load_configured_pools(json.dumps([pool]), pool["address"])
    assert loaded == [PoolMetadata.create(**pool)]

    with pytest.raises(ValueError, match="differ"):
        load_configured_pools(
            json.dumps([pool]),
            "0x0000000000000000000000000000000000000099",
        )


def test_load_configured_pools_rejects_duplicates():
    pool = {
        "address": "0x0000000000000000000000000000000000000011",
        "token0": "0x0000000000000000000000000000000000000001",
        "token1": "0x0000000000000000000000000000000000000002",
        "venue": "aerodrome",
        "pool_type": "xyk",
        "stable": False,
    }
    with pytest.raises(ValueError, match="duplicate"):
        load_configured_pools(json.dumps([pool, pool]))
