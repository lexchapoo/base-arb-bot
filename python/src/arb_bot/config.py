from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    chain_id: int = 8453
    base_http_rpc: str = "https://mainnet.base.org"
    watched_pool_addresses: str = ""
    watched_pools_json: str = ""
    shadow_observation_seconds: int = 3600
    shadow_poll_interval_seconds: int = 5
    database_url: str = "postgresql+asyncpg://arb:arb@localhost:5432/arb"
    dry_run: bool = True
    live_trading: bool = False
    auto_submit_routes: bool = True
    packed_multiroute_enabled: bool = True
    packed_max_candidates: int = 8
    min_profit_usd: float | None = None
    max_gas_usd: float | None = None
    max_route_hops: int = 3
    max_state_age_blocks: int = 1
    max_quote_evaluations: int = 24
    # Upper bound on quote/cycle evaluations run concurrently. Caps in-flight
    # RPC calls so parallel evaluation does not overwhelm the provider.
    quote_concurrency: int = 8
    rust_executor_url: str = "http://localhost:8081"
    quote_timeout_seconds: float = 4.0
    execution_rpc_timeout_seconds: float = 8.0
    uniswap_v3_quoter_v2: str = ""
    aerodrome_router: str = ""
    aerodrome_factory: str = ""
    executor_address: str = ""
    executor_owner_address: str = ""
    aave_pool: str = ""
    base_weth: str = ""
    aerodrome_adapter_address: str = ""
    uniswap_v3_adapter_address: str = ""
    # Base's protocol-defined GasPriceOracle predeploy; bytecode is verified before use.
    gas_price_oracle: str = "0x420000000000000000000000000000000000000F"


settings = Settings()
