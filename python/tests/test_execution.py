import pytest

from arb_bot.adapters.base import Quote
from arb_bot.config import settings
from arb_bot.event_graph import Cycle, LivePoolGraph, PoolMetadata
from arb_bot.execution import AaveSnapshot, ExecutionFinalizer, SimulationResult

A="0x0000000000000000000000000000000000000001"
B="0x0000000000000000000000000000000000000002"
P1="0x0000000000000000000000000000000000000011"
P2="0x0000000000000000000000000000000000000012"
AD1="0x0000000000000000000000000000000000000021"
AD2="0x0000000000000000000000000000000000000022"
EXEC="0x0000000000000000000000000000000000000031"
OWNER="0x0000000000000000000000000000000000000032"
AAVE="0x0000000000000000000000000000000000000033"
WETH="0x0000000000000000000000000000000000000034"
ORACLE="0x420000000000000000000000000000000000000F"

class FakeRegistry:
    @staticmethod
    def canonical_venue(v): return v
    def get(self, v): return object()

class FakeRuntime:
    def __init__(self, liquidity=10_000): self.liquidity=liquidity
    async def verify_contract(self, address): return None
    async def aave_snapshot(self, pool, asset):
        return AaveSnapshot(premium_bps=10, available_liquidity=self.liquidity, flashloan_enabled=True, a_token=AD1)
    async def l1_fee_upper_bound(self, oracle, tx_size): return 6
    def adapter_data(self, pool): return b""
    def checksum(self, address): return address
    def encode_start(self, **kwargs): return "0x1234567890", "0x"+"11"*32

class FakeRpc:
    async def chain_id(self): return 8453
    async def block_number(self): return 100
    async def simulate(self, tx): return SimulationResult(True,10,101,1,None,{})
    async def estimate_gas(self, tx): return 12
    async def gas_price(self): return 2
    async def nonce(self, address): return 1
    async def priority_fee(self): return 1
    async def pending_base_fee(self): return 1

class FakeConverter:
    async def convert(self, asset, native_wei):
        assert native_wei == 30
        return 30

@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings,"executor_address",EXEC)
    monkeypatch.setattr(settings,"executor_owner_address",OWNER)
    monkeypatch.setattr(settings,"aave_pool",AAVE)
    monkeypatch.setattr(settings,"base_weth",WETH)
    monkeypatch.setattr(settings,"gas_price_oracle",ORACLE)
    monkeypatch.setattr(settings,"uniswap_v3_adapter_address",AD1)
    monkeypatch.setattr(settings,"aerodrome_adapter_address",AD2)


def cycle_and_quotes():
    p1=PoolMetadata.create(address=P1,token0=A,token1=B,venue="uniswap-v3",pool_type="cl",fee=500)
    p2=PoolMetadata.create(address=P2,token0=A,token1=B,venue="aerodrome",pool_type="xyk",stable=False)
    cycle=Cycle((p1,p2),(A,B,A))
    quotes=[
        Quote("uniswap-v3",A,B,1000,2000,None,101,{}),
        Quote("aerodrome",B,A,2000,1200,None,101,{}),
    ]
    return cycle, quotes

@pytest.mark.asyncio
async def test_finalizer_uses_flash_premium_full_gas_and_l1_fee(configured):
    cycle,quotes=cycle_and_quotes()
    finalizer=ExecutionFinalizer(LivePoolGraph(),FakeRegistry())
    finalizer.contracts=FakeRuntime()
    finalizer.rpc=FakeRpc()
    finalizer.converter=FakeConverter()
    result=await finalizer.finalize(cycle,1000,1200,quotes,101)
    assert result.simulation_success is True
    assert result.flashloan_premium_units == 1
    assert result.l2_fee_wei == 24
    assert result.l1_fee_upper_bound_wei == 6
    assert result.total_native_fee_wei == 30
    assert result.gas_cost_asset_units == 30
    assert result.deterministic_net_profit_units == 169
    assert result.submission_eligible is True
    assert result.blockers == ()

@pytest.mark.asyncio
async def test_finalizer_blocks_insufficient_flash_liquidity(configured):
    cycle,quotes=cycle_and_quotes()
    finalizer=ExecutionFinalizer(LivePoolGraph(),FakeRegistry())
    finalizer.contracts=FakeRuntime(liquidity=999)
    finalizer.rpc=FakeRpc()
    finalizer.converter=FakeConverter()
    result=await finalizer.finalize(cycle,1000,1200,quotes,101)
    assert result.submission_eligible is False
    assert "insufficient_aave_flash_liquidity" in result.blockers
