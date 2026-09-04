from datetime import datetime
from decimal import Decimal
from sqlalchemy import BigInteger, Boolean, DateTime, Integer, Numeric, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class OpportunityRecord(Base):
    __tablename__ = "opportunities"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    observed_block: Mapped[int] = mapped_column(BigInteger)
    target_block: Mapped[int] = mapped_column(BigInteger)
    route_hash: Mapped[str] = mapped_column(String(66), index=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    expected_value_usd: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    net_profit_usd: Mapped[Decimal] = mapped_column(Numeric(24, 8))
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SubmissionRecord(Base):
    __tablename__ = "submissions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    submission_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    opportunity_id: Mapped[str] = mapped_column(String(96), index=True)
    provider: Mapped[str] = mapped_column(String(128), default="dry-run")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    accepted: Mapped[bool] = mapped_column(Boolean)
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PendingPoolEventRecord(Base):
    __tablename__ = "pending_pool_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    pool_address: Mapped[str] = mapped_column(String(42), index=True)
    topic0: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    transaction_hash: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    transaction_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_at_unix_ms: Mapped[int] = mapped_column(BigInteger)
    known_pool: Mapped[bool] = mapped_column(Boolean)
    affected_pools_json: Mapped[str] = mapped_column(Text, default="[]")
    raw_data: Mapped[str] = mapped_column(Text, default="0x")
    source_sender: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)
    tx_touched_pools_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RouteEvaluationRecord(Base):
    __tablename__ = "route_evaluations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    route_id: Mapped[str] = mapped_column(String(66), index=True)
    trigger_pool: Mapped[str] = mapped_column(String(42), index=True)
    pools_json: Mapped[str] = mapped_column(Text)
    tokens_json: Mapped[str] = mapped_column(Text)
    amount_in: Mapped[str | None] = mapped_column(String(96), nullable=True)
    amount_out: Mapped[str | None] = mapped_column(String(96), nullable=True)
    gross_profit_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    quote_gas_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ev_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    submission_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    blockers_json: Mapped[str] = mapped_column(Text, default="[]")
    quote_metadata_json: Mapped[str] = mapped_column(Text, default="[]")
    execution_json: Mapped[str] = mapped_column(Text, default="{}")
    executor_calldata: Mapped[str | None] = mapped_column(Text, nullable=True)
    simulation_gas_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    estimate_gas_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_native_fee_wei: Mapped[str | None] = mapped_column(String(96), nullable=True)
    gas_cost_asset_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # Which venue the route was priced against. A dedicated column, not just a key inside
    # execution_json, so a shadow report can GROUP BY it without parsing every row.
    flash_provider: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    flashloan_premium_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flashloan_premium_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    deterministic_net_profit_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    evaluation_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_disagreement: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    would_submit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class VerifiedPoolRecord(Base):
    __tablename__ = "verified_pools"
    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    factory: Mapped[str] = mapped_column(String(42), index=True)
    token0: Mapped[str] = mapped_column(String(42), index=True)
    token1: Mapped[str] = mapped_column(String(42), index=True)
    venue: Mapped[str] = mapped_column(String(32))
    pool_type: Mapped[str] = mapped_column(String(32))
    fee: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tick_spacing: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_verified_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CuratedPoolRecord(Base):
    """The operator-selected pool set the router is meant to run on.

    Separate from `verified_pools` on purpose: that table is the full discovery output
    (~28.6k rows on Base), while this is the small liquid subset curation chose. Keeping
    it durable is what makes a selection survive a restart when automatic discovery --
    which would otherwise rehydrate the graph -- is switched off.
    """

    __tablename__ = "curated_pools"
    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    token0: Mapped[str] = mapped_column(String(42), index=True)
    token1: Mapped[str] = mapped_column(String(42), index=True)
    venue: Mapped[str] = mapped_column(String(32))
    pool_type: Mapped[str] = mapped_column(String(32))
    fee: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    tick_spacing: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DiscoveryCheckpointRecord(Base):
    __tablename__ = "discovery_checkpoints"
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    factory: Mapped[str] = mapped_column(String(42))
    next_cursor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_scanned_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class OpportunityTelemetryRecord(Base):
    __tablename__ = "opportunity_telemetry"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    source_tx: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    trigger_pool: Mapped[str] = mapped_column(String(42), index=True)
    observed_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    state_version: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    state_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    affected_pools_json: Mapped[str] = mapped_column(Text, default="[]")
    candidate_routes_json: Mapped[str] = mapped_column(Text, default="[]")
    packed_batch_hash: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    deterministic_net_profit_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    expected_capture_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    decision: Mapped[str] = mapped_column(String(24), default="SKIP", index=True)
    decision_reason: Mapped[str] = mapped_column(Text, default="")
    tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    submission_provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    submission_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    receipt_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gas_used: Mapped[str | None] = mapped_column(String(96), nullable=True)
    effective_gas_price_wei: Mapped[str | None] = mapped_column(String(96), nullable=True)
    l1_fee_wei: Mapped[str | None] = mapped_column(String(96), nullable=True)
    actual_native_fee_wei: Mapped[str | None] = mapped_column(String(96), nullable=True)
    asset_balance_before: Mapped[str | None] = mapped_column(String(96), nullable=True)
    asset_balance_after: Mapped[str | None] = mapped_column(String(96), nullable=True)
    realized_profit_units_ex_gas: Mapped[str | None] = mapped_column(String(96), nullable=True)
    realized_net_profit_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    net_profit_exact: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_family: Mapped[str | None] = mapped_column(String(66), nullable=True, index=True)
    survival_probability_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_capture_probability_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shadow_action: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    shadow_confidence_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shadow_features_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
