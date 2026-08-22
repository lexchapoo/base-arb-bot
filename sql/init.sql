CREATE TABLE IF NOT EXISTS endpoint_benchmarks (
  id BIGSERIAL PRIMARY KEY,
  provider TEXT NOT NULL,
  rpc_url_hash TEXT NOT NULL,
  method TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  success BOOLEAN NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS pending_pool_events (
  id BIGSERIAL PRIMARY KEY,
  pool_address VARCHAR(42) NOT NULL,
  topic0 VARCHAR(66),
  transaction_hash VARCHAR(66),
  block_number BIGINT,
  transaction_index INTEGER,
  log_index INTEGER,
  observed_at_unix_ms BIGINT NOT NULL,
  known_pool BOOLEAN NOT NULL,
  affected_pools_json TEXT NOT NULL DEFAULT '[]',
  raw_data TEXT NOT NULL DEFAULT '0x',
  source_sender VARCHAR(42),
  tx_touched_pools_json TEXT NOT NULL DEFAULT '[]',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_pending_pool_events_pool_address ON pending_pool_events(pool_address);
CREATE INDEX IF NOT EXISTS ix_pending_pool_events_tx_hash ON pending_pool_events(transaction_hash);
CREATE INDEX IF NOT EXISTS ix_pending_pool_events_block_number ON pending_pool_events(block_number);
CREATE INDEX IF NOT EXISTS ix_pending_pool_events_source_sender ON pending_pool_events(source_sender);

CREATE TABLE IF NOT EXISTS route_evaluations (
  id BIGSERIAL PRIMARY KEY,
  route_id VARCHAR(66) NOT NULL,
  trigger_pool VARCHAR(42) NOT NULL,
  pools_json TEXT NOT NULL,
  tokens_json TEXT NOT NULL,
  amount_in VARCHAR(96),
  amount_out VARCHAR(96),
  gross_profit_units VARCHAR(96),
  quote_gas_units BIGINT,
  ev_ready BOOLEAN NOT NULL DEFAULT false,
  submission_eligible BOOLEAN NOT NULL DEFAULT false,
  blockers_json TEXT NOT NULL DEFAULT '[]',
  quote_metadata_json TEXT NOT NULL DEFAULT '[]',
  execution_json TEXT NOT NULL DEFAULT '{}',
  executor_calldata TEXT,
  simulation_gas_units BIGINT,
  estimate_gas_units BIGINT,
  total_native_fee_wei VARCHAR(96),
  gas_cost_asset_units VARCHAR(96),
  flashloan_premium_bps INTEGER,
  flashloan_premium_units VARCHAR(96),
  deterministic_net_profit_units VARCHAR(96),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_route_evaluations_route_id ON route_evaluations(route_id);
CREATE INDEX IF NOT EXISTS ix_route_evaluations_trigger_pool ON route_evaluations(trigger_pool);

CREATE TABLE IF NOT EXISTS verified_pools (
  address VARCHAR(42) PRIMARY KEY,
  source VARCHAR(32) NOT NULL,
  factory VARCHAR(42) NOT NULL,
  token0 VARCHAR(42) NOT NULL,
  token1 VARCHAR(42) NOT NULL,
  venue VARCHAR(32) NOT NULL,
  pool_type VARCHAR(32) NOT NULL,
  fee INTEGER,
  stable BOOLEAN,
  tick_spacing INTEGER,
  last_verified_block BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_verified_pools_source ON verified_pools(source);
CREATE INDEX IF NOT EXISTS ix_verified_pools_factory ON verified_pools(factory);
CREATE INDEX IF NOT EXISTS ix_verified_pools_token0 ON verified_pools(token0);
CREATE INDEX IF NOT EXISTS ix_verified_pools_token1 ON verified_pools(token1);

CREATE TABLE IF NOT EXISTS discovery_checkpoints (
  source VARCHAR(32) PRIMARY KEY,
  factory VARCHAR(42) NOT NULL,
  next_cursor BIGINT NOT NULL,
  last_scanned_block BIGINT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


CREATE TABLE IF NOT EXISTS opportunity_telemetry (
  id BIGSERIAL PRIMARY KEY,
  opportunity_id VARCHAR(96) UNIQUE NOT NULL,
  source_tx VARCHAR(66),
  trigger_pool VARCHAR(42) NOT NULL,
  observed_block BIGINT,
  state_version VARCHAR(66),
  state_sequence BIGINT,
  affected_pools_json TEXT NOT NULL DEFAULT '[]',
  candidate_routes_json TEXT NOT NULL DEFAULT '[]',
  packed_batch_hash VARCHAR(66),
  deterministic_net_profit_units VARCHAR(96),
  expected_capture_units VARCHAR(96),
  decision VARCHAR(24) NOT NULL DEFAULT 'SKIP',
  decision_reason TEXT NOT NULL DEFAULT '',
  tx_hash VARCHAR(66),
  submission_provider VARCHAR(128),
  submission_latency_ms INTEGER,
  receipt_status INTEGER,
  gas_used VARCHAR(96),
  effective_gas_price_wei VARCHAR(96),
  l1_fee_wei VARCHAR(96),
  actual_native_fee_wei VARCHAR(96),
  asset_balance_before VARCHAR(96),
  asset_balance_after VARCHAR(96),
  realized_profit_units_ex_gas VARCHAR(96),
  realized_net_profit_units VARCHAR(96),
  net_profit_exact BOOLEAN NOT NULL DEFAULT false,
  failure_reason TEXT,
  route_family VARCHAR(66),
  survival_probability_bps INTEGER,
  route_capture_probability_bps INTEGER,
  shadow_action VARCHAR(12),
  shadow_confidence_bps INTEGER,
  shadow_features_json TEXT NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_source_tx ON opportunity_telemetry(source_tx);
CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_trigger_pool ON opportunity_telemetry(trigger_pool);
CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_state_sequence ON opportunity_telemetry(state_sequence);
CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_tx_hash ON opportunity_telemetry(tx_hash);
CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_route_family ON opportunity_telemetry(route_family);
CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_shadow_action ON opportunity_telemetry(shadow_action);
