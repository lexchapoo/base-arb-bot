import pytest

from arb_bot.batch_execution import PackedBatchFinalizer, compatible_groups
from arb_bot.config import settings
from arb_bot.execution import SimulationResult
from arb_bot.route_engine import RouteEvaluation

A="0x0000000000000000000000000000000000000001"
B="0x0000000000000000000000000000000000000002"
AD="0x0000000000000000000000000000000000000010"
EXEC="0x0000000000000000000000000000000000000020"
OWNER="0x0000000000000000000000000000000000000021"


def row(route_id, amount, ev, min_profit, target=100):
    pc={"asset":A,"amount":amount,"target_block":target,"deadline":2**64-1,"min_profit":min_profit,"legs":[
        {"adapter":AD,"token_in":A,"token_out":B,"min_out":1,"data":"0x"},
        {"adapter":AD,"token_in":B,"token_out":A,"min_out":1,"data":"0x"},
    ]}
    return RouteEvaluation(route_id,(),(A,B,A),amount,amount+100,100,(),None,True,True,(),(),{
        "deterministic_net_profit_units":ev,"packed_candidate":pc
    })

class Runtime:
    def checksum(self,a): return a
    def encode_start_packed(self, *, executor, packed): return "0x1234567890", "0x"+"ab"*32
    async def l1_fee_upper_bound(self, oracle, tx_size): return 10

class Rpc:
    async def simulate(self,tx): return SimulationResult(True,100,None,None,None,{})
    async def estimate_gas(self,tx): return 120
    async def gas_price(self): return 2
    async def nonce(self,a): return 1
    async def priority_fee(self): return 1
    async def pending_base_fee(self): return 1

class Converter:
    async def convert(self,asset,native):
        assert native == 250
        return 250

class Finalizer:
    def __init__(self): self.rpc=Rpc(); self.converter=Converter(); self.runtime=Runtime()
    def _runtime(self): return self.runtime


def test_compatible_groups_never_mix_flash_amounts():
    groups=compatible_groups([row("r1",1000,50,500),row("r2",2000,100,600),row("r3",1000,40,450)])
    assert [r.route_id for r in groups[0]] == ["r2"]
    assert {r.route_id for r in groups[1]} == {"r1","r3"}

@pytest.mark.asyncio
async def test_exact_packed_simulation_and_cost_gate(monkeypatch):
    monkeypatch.setattr(settings,"executor_address",EXEC)
    monkeypatch.setattr(settings,"executor_owner_address",OWNER)
    monkeypatch.setattr(settings,"packed_max_candidates",8)
    plan=await PackedBatchFinalizer(Finalizer()).finalize([row("r1",1000,500,700),row("r2",1000,400,600)])
    assert plan.simulation_success is True
    assert plan.submission_eligible is True
    assert plan.candidate_route_ids == ("r1","r2")
    assert plan.gas_cost_asset_units == 250
    assert plan.deterministic_net_profit_units == 350

@pytest.mark.asyncio
async def test_low_profit_fallback_is_trimmed(monkeypatch):
    monkeypatch.setattr(settings,"executor_address",EXEC)
    monkeypatch.setattr(settings,"executor_owner_address",OWNER)
    plan=await PackedBatchFinalizer(Finalizer()).finalize([row("r1",1000,500,700),row("r2",1000,10,200)])
    assert plan.submission_eligible is True
    assert plan.candidate_route_ids == ("r1",)
    assert any(x.startswith("trimmed_low_ev_candidate:r2") for x in plan.blockers)

@pytest.mark.asyncio
async def test_packed_batch_adaptive_gate_rejects_stale_trigger(monkeypatch):
    monkeypatch.setattr(settings,"executor_address",EXEC)
    monkeypatch.setattr(settings,"executor_owner_address",OWNER)
    monkeypatch.setattr(settings,"adaptive_submission_enabled",True)
    monkeypatch.setattr(settings,"opportunity_survival_window_ms",500)
    monkeypatch.setattr(settings,"expected_execution_latency_ms",100)
    monkeypatch.setattr(settings,"inclusion_probability_bps",10000)
    monkeypatch.setattr(settings,"expected_failure_cost_asset_units",0)
    monkeypatch.setattr(settings,"submission_safety_margin_asset_units",0)
    plan=await PackedBatchFinalizer(Finalizer()).finalize(
        [row("r1",1000,500,700)], observed_at_unix_ms=1000, now_unix_ms=2000
    )
    assert plan.simulation_success is True
    assert plan.submission_eligible is False
    assert plan.adaptive_submission["survival_probability_bps"] == 0
    assert "adaptive_expected_capture_below_threshold" in plan.blockers


@pytest.mark.asyncio
async def test_trim_drops_the_binding_min_profit_not_the_tail(monkeypatch):
    """The menu floor is min(min_profit); trimming must remove that candidate.

    r_low ranks first on its own post-gas EV (its route-level gas was small) but carries the
    lowest on-chain profit floor. Trimming the tail would drop r_high and then r_low, reporting
    no profitable batch -- even though the r_high-only menu clears the exact packed gas cost.
    """
    monkeypatch.setattr(settings, "executor_address", EXEC)
    monkeypatch.setattr(settings, "executor_owner_address", OWNER)
    monkeypatch.setattr(settings, "adaptive_submission_enabled", False)
    plan = await PackedBatchFinalizer(Finalizer()).finalize(
        [row("r_low", 1000, 800, 200), row("r_high", 1000, 400, 900)]
    )
    assert plan.submission_eligible is True
    assert plan.candidate_route_ids == ("r_high",)
    assert plan.deterministic_net_profit_units == 650  # 900 - 250 exact packed gas
    assert any(x.startswith("trimmed_low_ev_candidate:r_low") for x in plan.blockers)
