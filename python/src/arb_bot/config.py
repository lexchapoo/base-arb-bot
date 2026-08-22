from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    chain_id: int = 8453
    base_http_rpc: str = "https://mainnet.base.org"
    base_http_rpcs: str = ""
    rpc_consensus_required: bool = True
    rpc_probe_timeout_seconds: float = 2.5
    receipt_poll_ms: int = 1000
    receipt_timeout_seconds: int = 45
    database_url: str = "postgresql+asyncpg://arb:arb@localhost:5432/arb"
    dry_run: bool = True
    live_trading: bool = False
    auto_submit_routes: bool = True
    packed_multiroute_enabled: bool = True
    packed_max_candidates: int = 8
    adaptive_submission_enabled: bool = True
    opportunity_survival_window_ms: int = 800
    expected_execution_latency_ms: int = 120
    inclusion_probability_bps: int = 9000
    expected_failure_cost_asset_units: int = 0
    submission_safety_margin_asset_units: int = 0
    shadow_policy_enabled: bool = True
    shadow_min_samples: int = 20
    shadow_history_limit: int = 500
    shadow_now_capture_bps: int = 6000
    shadow_now_survival_bps: int = 7000
    shadow_wait_survival_bps: int = 3500
    min_profit_usd: float | None = None
    max_gas_usd: float | None = None
    max_route_hops: int = 3
    max_state_age_blocks: int = 1
    max_quote_evaluations: int = 24
    # Upper bound on cycle evaluations running concurrently, so fan-out cannot overwhelm the
    # RPC provider.
    quote_concurrency: int = 8
    # Hard wall-clock budget for one /route-trigger evaluation pass. Auto-discovery registers
    # the whole verified Base pool set, so the cycle count per trigger is not bounded by
    # config -- this is what keeps a trigger from outliving the opportunity it was priced for.
    route_evaluation_budget_seconds: float = 6.0
    # After the coarse candidate grid, quote this many extra sizes between the
    # best size's neighbors (one more batched round) to sharpen the optimum.
    # 0 disables refinement.
    size_refinement_points: int = 4
    rust_executor_url: str = "http://localhost:8081"
    quote_timeout_seconds: float = 4.0
    execution_rpc_timeout_seconds: float = 8.0
    uniswap_v3_quoter_v2: str = ""
    aerodrome_router: str = ""
    aerodrome_factory: str = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
    uniswap_v3_factory: str = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
    auto_pool_discovery_enabled: bool = True
    pool_discovery_refresh_seconds: int = 60
    pool_discovery_from_block: int = 0
    pool_discovery_log_chunk_blocks: int = 20_000
    pool_discovery_concurrency: int = 16
    pool_discovery_token_allowlist: str = ""
    executor_address: str = ""
    executor_owner_address: str = ""
    aave_pool: str = ""
    base_weth: str = ""
    aerodrome_adapter_address: str = ""
    uniswap_v3_adapter_address: str = ""
    # Aerodrome Slipstream (concentrated liquidity). Left blank deliberately: populate only
    # with independently verified Base deployments, then the adapter registers itself.
    aerodrome_slipstream_quoter: str = ""
    aerodrome_slipstream_router: str = ""
    aerodrome_slipstream_factory: str = ""
    aerodrome_slipstream_adapter_address: str = ""
    # Base's protocol-defined GasPriceOracle predeploy; bytecode is verified before use.
    gas_price_oracle: str = "0x420000000000000000000000000000000000000F"


settings = Settings()
