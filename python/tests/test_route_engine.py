import pytest
from arb_bot.adapters.base import Quote
from arb_bot.event_graph import LivePoolGraph, PoolMetadata
from arb_bot.route_engine import RouteOptimizer
from arb_bot.execution import ExecutionFinalization, conservative_eip1559_signed_size

A="0x0000000000000000000000000000000000000001"
B="0x0000000000000000000000000000000000000002"
P1="0x0000000000000000000000000000000000000011"
P2="0x0000000000000000000000000000000000000012"

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


def test_conservative_eip1559_size_increases_with_calldata():
    small=conservative_eip1559_signed_size(8453,1,1,2,21000,P1,"0x")
    large=conservative_eip1559_signed_size(8453,1,1,2,21000,P1,"0x"+"11"*256)
    assert large > small
