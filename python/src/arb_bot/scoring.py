from __future__ import annotations
from decimal import Decimal
from .models import RouteCandidate, ScoreResult

class RiskGate:
    def __init__(self, min_profit_usd: Decimal | None, max_gas_usd: Decimal | None, max_hops: int, max_state_age_blocks: int):
        self.min_profit_usd=min_profit_usd
        self.max_gas_usd=max_gas_usd
        self.max_hops=max_hops
        self.max_state_age_blocks=max_state_age_blocks

    def score(self, c: RouteCandidate) -> ScoreResult:
        net = c.gross_profit_usd-c.gas_cost_usd-c.hedge_cost_usd-c.inventory_risk_usd
        ev=(c.success_probability*net
            - c.revert_probability*c.revert_cost_usd
            - c.stale_probability*c.stale_loss_usd
            - c.leakage_probability*c.opportunity_loss_usd)
        age=max(0, c.target_block-c.observed_block)
        reasons=[]
        if c.chain_id != 8453: reasons.append("unsupported_chain")
        if self.min_profit_usd is not None and net < self.min_profit_usd: reasons.append("net_profit_below_floor")
        if ev <= Decimal("0"): reasons.append("non_positive_expected_value")
        if self.max_gas_usd is not None and c.gas_cost_usd > self.max_gas_usd: reasons.append("gas_above_limit")
        if c.route_hops > self.max_hops: reasons.append("too_many_route_hops")
        if age > self.max_state_age_blocks: reasons.append("stale_state")
        total_p=c.success_probability+c.revert_probability+c.stale_probability+c.leakage_probability
        if total_p > Decimal("1.000001"): reasons.append("invalid_probability_mass")
        return ScoreResult(opportunity_id=c.opportunity_id,approved=not reasons,net_profit_usd=net,expected_value_usd=ev,state_age_blocks=age,reasons=reasons)
