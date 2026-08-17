from decimal import Decimal
from arb_bot.models import RouteCandidate
from arb_bot.scoring import RiskGate

def candidate(**overrides):
    base=dict(opportunity_id="x",target_block=10,observed_block=10,asset="0x0",amount_in="1",gross_profit_usd="3",gas_cost_usd="0.2",success_probability="0.9",revert_probability="0.05",stale_probability="0.03",leakage_probability="0.02",revert_cost_usd="0.2",stale_loss_usd="0.4",opportunity_loss_usd="1",route_hops=2,route_hash="0x"+"11"*32)
    base.update(overrides)
    return RouteCandidate(**base)

def test_profitable_route_is_approved():
    out=RiskGate(Decimal("1"),Decimal("0.5"),3,1).score(candidate())
    assert out.approved and out.expected_value_usd > 0

def test_stale_route_rejected():
    out=RiskGate(Decimal("1"),Decimal("0.5"),3,1).score(candidate(target_block=13))
    assert "stale_state" in out.reasons
