import os

from pydantic_settings import BaseSettings, SettingsConfigDict

# `.env` resolves against the process working directory, so what the service loads depends
# on where it was started from. That is fine for the container (it starts in the app root)
# and actively harmful for the test suite, whose results silently changed depending on
# whether pytest ran from the repo root or from python/. ARB_BOT_ENV_FILE makes the choice
# explicit; the tests point it at a path that does not exist.
ENV_FILE = os.environ.get("ARB_BOT_ENV_FILE", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")
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
    # Block tag every quote and balance read in the sizing path is priced against.
    #
    # "pending" is the production setting and the premise of the whole strategy: routes are
    # priced against Base Flashblocks pre-confirmation state, which is what makes the edge
    # available at all. "latest" prices confirmed state instead -- strictly worse
    # information, and it forfeits that edge -- but a node that cannot serve `pending`
    # quickly makes every quote time out, and a slow correct answer is worth less than a
    # fast approximate one when the alternative is no answer. Only widen this to "latest"
    # deliberately, and expect the results to describe a market one block behind the one
    # you would actually trade into.
    quote_block_tag: str = "pending"
    quote_timeout_seconds: float = 4.0
    execution_rpc_timeout_seconds: float = 8.0
    uniswap_v3_quoter_v2: str = ""
    aerodrome_router: str = ""
    aerodrome_factory: str = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
    uniswap_v3_factory: str = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
    # Shared secret required by the pool-curation endpoints, which replace the durable
    # curated set and the live routing graph. Empty means those endpoints are refused
    # outright: an unauthenticated caller able to reach the port could otherwise redirect
    # route evaluation onto pools of their choosing, or empty the graph and halt trading.
    operator_api_token: str = ""
    auto_pool_discovery_enabled: bool = True
    pool_discovery_refresh_seconds: int = 60
    pool_discovery_from_block: int = 0
    pool_discovery_log_chunk_blocks: int = 20_000
    pool_discovery_concurrency: int = 16
    pool_discovery_token_allowlist: str = ""
    # Separate endpoint for discovery's verification eth_calls (get_code/token0/factory).
    # Log reads need history, verification only needs a synced head, and the verification
    # fan-out is what actually trips rate limits -- so pointing this at a local node while
    # BASE_HTTP_RPC stays on an archive provider is the configuration that scales. Empty
    # means "same endpoint as the log reads", which is the previous behaviour.
    pool_discovery_call_rpc_url: str = ""
    # Split-RPC discovery only: how far the verification endpoint may trail the log
    # endpoint before its get_code/token0 answers are treated as untrustworthy. A lagging
    # node reports recently created pools as non-existent, which silently drops them.
    # Inert unless pool_discovery_call_rpc_url is set: with one endpoint there is no lag.
    pool_discovery_max_call_lag_blocks: int = 64
    # Which venue routes borrow their principal from. "aave" keeps the historical behaviour
    # (Aave v3, 5bp premium); "morpho" borrows from Morpho Blue, which charges no premium at
    # all and, on Base, holds roughly an order of magnitude more WETH. Selected per submission
    # rather than baked into the executor, so a shadow run can A/B the two.
    flash_provider: str = "aave"
    # Shadow only: price every route against the *other* provider as well and record the
    # comparison alongside the real evaluation. Doubles the RPC cost of an evaluation, so it
    # is refused outside DRY_RUN -- it buys a number nothing acts on, which is exactly what a
    # shadow run is for and exactly what live trading should not pay for.
    shadow_compare_providers: bool = False
    # Morpho Blue singleton. Required only when flash_provider is "morpho"; the executor
    # refuses Morpho routes rather than falling back to Aave when this is unset.
    morpho_address: str = ""
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

if settings.quote_block_tag not in {"pending", "latest"}:
    raise ValueError(
        f"QUOTE_BLOCK_TAG must be 'pending' or 'latest', got {settings.quote_block_tag!r}"
    )
