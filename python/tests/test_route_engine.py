import pytest
from arb_bot.adapters.base import Quote
from arb_bot.event_graph import LivePoolGraph, PoolMetadata, Cycle
from arb_bot.route_engine import RouteOptimizer
from arb_bot.execution import ExecutionFinalization, conservative_eip1559_signed_size

A="0x0000000000000000000000000000000000000001"
B="0x0000000000000000000000000000000000000002"
C="0x0000000000000000000000000000000000000003"
P1="0x0000000000000000000000000000000000000011"
P2="0x0000000000000000000000000000000000000012"
P3="0x0000000000000000000000000000000000000013"


def _pm(addr, t0, t1, venue):
    return PoolMetadata.create(address=addr, token0=t0, token1=t1, venue=venue, pool_type="test")


class FakeBatchAdapter:
    """Adapter that supports the multicall batching path (encode/decode)."""
    def __init__(self, venue, numerator, denominator):
        self.venue = venue; self.n = numerator; self.d = denominator

    def encode_quote(self, token_in, token_out, amount_in, **kwargs):
        return ("0x000000000000000000000000000000000000dEaD", b"\x00")

    def decode_quote(self, return_data, token_in, token_out, amount_in, **kwargs):
        return Quote(self.venue, token_in, token_out, amount_in, amount_in * self.n // self.d, 10, kwargs.get("block_number"), {"batched": True})

    async def quote_exact_input(self, token_in, token_out, amount_in, **kwargs):
        return self.decode_quote(b"", token_in, token_out, amount_in, **kwargs)


class BatchRegistry:
    def __init__(self):
        self.adapters = {"v1": FakeBatchAdapter("v1", 2, 1), "v2": FakeBatchAdapter("v2", 3, 5)}
    @staticmethod
    def canonical_venue(v): return v
    def get(self, v): return self.adapters.get(v)


class FakeMulticall:
    """Records calls; returns a fixed (success, returndata) list for every request."""
    def __init__(self, value: int):
        self._rd = value.to_bytes(32, "big")
        self.last_calls = None
    async def aggregate3(self, calls, block_identifier="pending"):
        self.last_calls = calls
        return [(True, self._rd) for _ in calls]

class FakeAdapter:
    def __init__(self, venue, numerator, denominator):
        self.venue=venue; self.n=numerator; self.d=denominator
    async def quote_exact_input(self, token_in, token_out, amount_in, **kwargs):
        return Quote(self.venue, token_in, token_out, amount_in, amount_in*self.n//self.d, 10, 123, {"test_fixture":True})

class FakeRegistry:
    def __init__(self):
        self.adapters={"v1":FakeAdapter("v1",2,1),"v2":FakeAdapter("v2",3,5)}
    @staticmethod
    def canonical_venue(v): return v
    def get(self,v): return self.adapters.get(v)

class FakeBalance:
    async def token_balance(self, token, account, block_identifier="pending"):
        return 1024

class FakeFinalizer:
    async def finalize(self, cycle, amount_in, amount_out, quotes, observed_block):
        return ExecutionFinalization(
            calldata="0x1234",
            route_hash="0x"+"22"*32,
            target_block=observed_block,
            deadline=(1<<64)-1,
            simulation_gas_units=123456,
            estimate_gas_units=124000,
            gas_price_wei=10,
            l2_fee_wei=1240000,
            l1_fee_upper_bound_wei=1000,
            total_native_fee_wei=1241000,
            gas_cost_asset_units=2,
            flashloan_premium_bps=5,
            flashloan_premium_units=1,
            available_flash_liquidity=10_000,
            deterministic_net_profit_units=10,
            simulation_success=True,
            submission_eligible=True,
            blockers=(),
        )

@pytest.mark.asyncio
async def test_affected_cycle_is_quoted_sized_and_finalized():
    graph=LivePoolGraph()
    p1=PoolMetadata.create(address=P1,token0=A,token1=B,venue="v1",pool_type="test")
    p2=PoolMetadata.create(address=P2,token0=A,token1=B,venue="v2",pool_type="test")
    graph.register(p1); graph.register(p2)
    optimizer=RouteOptimizer(graph,FakeRegistry(),FakeBalance(),max_quote_evaluations=12,execution_finalizer=FakeFinalizer())
    results=await optimizer.evaluate_affected([P1,P2],2,456)
    profitable=[r for r in results if r.gross_profit_units is not None and r.gross_profit_units>0]
    assert profitable
    best=profitable[0]
    assert best.amount_in is not None and best.amount_in > 0
    assert best.amount_out is not None and best.amount_out > best.amount_in
    assert best.ev_ready is True
    assert best.submission_eligible is True
    assert best.execution["calldata"] == "0x1234"
    assert best.execution["deterministic_net_profit_units"] == 10

@pytest.mark.asyncio
async def test_missing_live_execution_config_is_explicit_blocker():
    graph=LivePoolGraph()
    graph.register(PoolMetadata.create(address=P1,token0=A,token1=B,venue="v1",pool_type="test"))
    graph.register(PoolMetadata.create(address=P2,token0=A,token1=B,venue="v2",pool_type="test"))
    optimizer=RouteOptimizer(graph,FakeRegistry(),FakeBalance(),max_quote_evaluations=12)
    results=await optimizer.evaluate_affected([P1,P2],2)
    profitable=[r for r in results if r.gross_profit_units is not None and r.gross_profit_units>0]
    assert profitable
    assert any(b.startswith("missing_config:") for b in profitable[0].blockers)
    assert profitable[0].submission_eligible is False


def test_graph_discovers_two_hop_cycle_touching_affected_pool():
    graph=LivePoolGraph()
    graph.register(PoolMetadata.create(address=P1,token0=A,token1=B,venue="v1",pool_type="test"))
    graph.register(PoolMetadata.create(address=P2,token0=A,token1=B,venue="v2",pool_type="test"))
    cycles=graph.cycles_for_affected([P1],2)
    assert any(c.hops==2 and P1 in [p.address for p in c.pools] and P2 in [p.address for p in c.pools] for c in cycles)


def test_reject_affected_emits_candidates_without_live_quotes():
    graph=LivePoolGraph()
    graph.register(PoolMetadata.create(address=P1,token0=A,token1=B,venue="v1",pool_type="test"))
    graph.register(PoolMetadata.create(address=P2,token0=A,token1=B,venue="v2",pool_type="test"))
    optimizer=RouteOptimizer(graph,FakeRegistry(),FakeBalance(),enable_execution_finalizer=False)
    results=optimizer.reject_affected([P1,P2],["missing_config:EXECUTOR_ADDRESS"],2)
    assert results
    assert all(result.amount_in is None for result in results)
    assert all(result.blockers == ("missing_config:EXECUTOR_ADDRESS",) for result in results)


def test_conservative_eip1559_size_increases_with_calldata():
    small=conservative_eip1559_signed_size(8453,1,1,2,21000,P1,"0x")
    large=conservative_eip1559_signed_size(8453,1,1,2,21000,P1,"0x"+"11"*256)
    assert large > small


def test_refine_amounts_samples_between_neighbors():
    grid=[1,4,16,64]
    out=RouteOptimizer._refine_amounts(grid,16,3)   # neighbours 4 and 64
    assert out==sorted(out)
    assert out and all(4 < a < 64 for a in out)
    assert 16 not in out


def test_refine_amounts_disabled_or_unknown_best():
    assert RouteOptimizer._refine_amounts([1,2,4],4,0)==[]      # points=0
    assert RouteOptimizer._refine_amounts([1,2,4],99,3)==[]     # best not in grid


def test_dedup_collapses_rotations_keeps_reverse():
    p1=_pm(P1,A,B,"v1"); p2=_pm(P2,B,C,"v2"); p3=_pm(P3,C,A,"v3")
    ring=Cycle((p1,p2,p3),(A,B,C,A))          # A->B->C->A
    rotation=Cycle((p2,p3,p1),(B,C,A,B))      # same directed ring, different entry
    reverse=Cycle((p3,p2,p1),(A,C,B,A))       # A->C->B->A, a different trade
    out=RouteOptimizer._dedup_cycles([ring,rotation,reverse])
    keys={RouteOptimizer._route_id(c) for c in out}
    # ring + rotation collapse to one; reverse is kept
    assert len(out)==2
    assert RouteOptimizer._route_id(reverse) in keys


@pytest.mark.asyncio
async def test_prefetch_balances_batches_unique_balanceof():
    graph=LivePoolGraph()
    graph.register(_pm(P1,A,B,"v1")); graph.register(_pm(P2,A,B,"v2"))
    optimizer=RouteOptimizer(graph,BatchRegistry())
    cycles=optimizer._dedup_cycles(graph.cycles_for_affected([P1,P2],2))
    optimizer._multicall_client=FakeMulticall(1000)
    balances=await optimizer._prefetch_balances(cycles)
    unique_pairs={(c.tokens[0],c.pools[0].address) for c in cycles}
    assert set(balances.keys())==unique_pairs
    assert all(v==1000 for v in balances.values())
    # one multicall request, every call is a balanceOf(address) (selector 0x70a08231)
    assert optimizer._multicall_client.last_calls is not None
    assert all(call_data[:4].hex()=="70a08231" for _,call_data in optimizer._multicall_client.last_calls)


@pytest.mark.asyncio
async def test_batched_path_quotes_and_finds_profit():
    graph=LivePoolGraph()
    graph.register(_pm(P1,A,B,"v1")); graph.register(_pm(P2,A,B,"v2"))
    optimizer=RouteOptimizer(graph,BatchRegistry(),execution_finalizer=FakeFinalizer())
    optimizer._multicall_client=FakeMulticall(10**9)  # non-zero balance + quote returndata
    results=await optimizer.evaluate_affected([P1,P2],2,456)
    profitable=[r for r in results if r.gross_profit_units is not None and r.gross_profit_units>0]
    assert profitable
    best=profitable[0]
    assert best.amount_out is not None and best.amount_out>best.amount_in
    # metadata proves the batched decode path produced the quotes
    assert all(m.get("batched") for m in best.quote_metadata)
