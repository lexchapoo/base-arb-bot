from __future__ import annotations
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator, model_validator

from .event_graph import canonical_venue


def _address(value: str) -> str:
    value = value.strip().lower()
    if not value.startswith("0x") or len(value) != 42:
        raise ValueError("must be a 20-byte hex EVM address")
    int(value[2:], 16)
    return value


class RouteCandidate(BaseModel):
    opportunity_id: str
    chain_id: int = 8453
    target_block: int
    observed_block: int
    asset: str
    amount_in: str
    gross_profit_usd: Decimal
    gas_cost_usd: Decimal
    hedge_cost_usd: Decimal = Decimal("0")
    inventory_risk_usd: Decimal = Decimal("0")
    success_probability: Decimal = Field(ge=0, le=1)
    revert_probability: Decimal = Field(ge=0, le=1)
    stale_probability: Decimal = Field(ge=0, le=1)
    leakage_probability: Decimal = Field(ge=0, le=1)
    revert_cost_usd: Decimal = Decimal("0")
    stale_loss_usd: Decimal = Decimal("0")
    opportunity_loss_usd: Decimal = Decimal("0")
    route_hops: int = Field(ge=1)
    route_hash: str

    @field_validator("route_hash")
    @classmethod
    def valid_hash(cls, value: str) -> str:
        if not value.startswith("0x") or len(value) != 66:
            raise ValueError("route_hash must be a 32-byte hex value")
        return value.lower()


class ScoreResult(BaseModel):
    opportunity_id: str
    approved: bool
    net_profit_usd: Decimal
    expected_value_usd: Decimal
    state_age_blocks: int
    reasons: list[str]


class ExecutionEnvelope(BaseModel):
    candidate: RouteCandidate
    score: ScoreResult
    calldata: str = "0x"


class PoolRegistration(BaseModel):
    address: str
    token0: str
    token1: str
    venue: str
    pool_type: str
    fee: int | None = Field(default=None, ge=0)
    stable: bool | None = None
    # Slipstream's pool key. Quoting rejects a Slipstream pool without it (route_engine
    # raises missing_tick_spacing), so a registration that omits it produces a pool that
    # is in the graph but can never be routed through -- silently dead weight.
    tick_spacing: int | None = None

    @field_validator("address", "token0", "token1")
    @classmethod
    def valid_address(cls, value: str) -> str:
        return _address(value)

    @model_validator(mode="after")
    def routing_key_present(self) -> "PoolRegistration":
        """Reject registrations the quoter would refuse to route through.

        Failing at the API boundary turns an unusable pool into a 422 the caller can see,
        instead of a graph entry that only reveals itself as dead at evaluation time.
        """
        venue = canonical_venue(self.venue)
        if venue == "aerodrome-slipstream" and self.tick_spacing is None:
            raise ValueError("aerodrome-slipstream pools require tick_spacing")
        if venue == "uniswap-v3" and self.fee is None:
            raise ValueError("uniswap-v3 pools require fee")
        if venue == "aerodrome" and self.stable is None:
            raise ValueError("aerodrome pools require stable")
        return self


class PendingLogTrigger(BaseModel):
    pool_address: str
    topic0: str | None = None
    transaction_hash: str | None = None
    block_number: int | None = None
    transaction_index: int | None = None
    log_index: int | None = None
    observed_at_unix_ms: int
    state_version: str | None = None
    state_sequence: int | None = None
    raw_data: str = "0x"
    source_sender: str | None = None
    tx_touched_pools: list[str] = Field(default_factory=list)

    @field_validator("pool_address", "source_sender")
    @classmethod
    def valid_pool(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _address(value)

    @field_validator("tx_touched_pools")
    @classmethod
    def valid_touched_pools(cls, values: list[str]) -> list[str]:
        return sorted({_address(v) for v in values})
