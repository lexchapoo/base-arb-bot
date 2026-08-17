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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_pending_pool_events_pool_address ON pending_pool_events(pool_address);
CREATE INDEX IF NOT EXISTS ix_pending_pool_events_tx_hash ON pending_pool_events(transaction_hash);
CREATE INDEX IF NOT EXISTS ix_pending_pool_events_block_number ON pending_pool_events(block_number);

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
  evaluation_latency_ms INTEGER,
  provider_disagreement BOOLEAN,
  would_submit BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_route_evaluations_route_id ON route_evaluations(route_id);
CREATE INDEX IF NOT EXISTS ix_route_evaluations_trigger_pool ON route_evaluations(trigger_pool);
