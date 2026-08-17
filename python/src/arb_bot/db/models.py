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
    flashloan_premium_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    flashloan_premium_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    deterministic_net_profit_units: Mapped[str | None] = mapped_column(String(96), nullable=True)
    evaluation_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_disagreement: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    would_submit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
