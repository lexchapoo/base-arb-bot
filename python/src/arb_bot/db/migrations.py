from sqlalchemy import text
from .session import engine

_STATEMENTS = [
    "ALTER TABLE pending_pool_events ADD COLUMN IF NOT EXISTS source_sender VARCHAR(42)",
    "ALTER TABLE pending_pool_events ADD COLUMN IF NOT EXISTS tx_touched_pools_json TEXT NOT NULL DEFAULT '[]'",
    "CREATE INDEX IF NOT EXISTS ix_pending_pool_events_source_sender ON pending_pool_events(source_sender)",
    "ALTER TABLE verified_pools ADD COLUMN IF NOT EXISTS tick_spacing INTEGER",
    # Present in sql/init.sql since the initial commit; re-added here for databases
    # created from a schema revision that dropped them.
    "ALTER TABLE route_evaluations ADD COLUMN IF NOT EXISTS evaluation_latency_ms INTEGER",
    "ALTER TABLE route_evaluations ADD COLUMN IF NOT EXISTS provider_disagreement BOOLEAN",
    "ALTER TABLE route_evaluations ADD COLUMN IF NOT EXISTS would_submit BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS route_family VARCHAR(66)",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS survival_probability_bps INTEGER",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS route_capture_probability_bps INTEGER",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS shadow_action VARCHAR(12)",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS shadow_confidence_bps INTEGER",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS shadow_features_json TEXT NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_route_family ON opportunity_telemetry(route_family)",
    "CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_shadow_action ON opportunity_telemetry(shadow_action)",
    # Durable home for the curated (operator-selected) pool set, so it survives a restart
    # with AUTO_POOL_DISCOVERY_ENABLED=false.
    """CREATE TABLE IF NOT EXISTS curated_pools (
         address VARCHAR(42) PRIMARY KEY,
         token0 VARCHAR(42) NOT NULL,
         token1 VARCHAR(42) NOT NULL,
         venue VARCHAR(32) NOT NULL,
         pool_type VARCHAR(32) NOT NULL,
         fee INTEGER,
         stable BOOLEAN,
         tick_spacing INTEGER,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
       )""",
    "CREATE INDEX IF NOT EXISTS ix_curated_pools_token0 ON curated_pools(token0)",
    "CREATE INDEX IF NOT EXISTS ix_curated_pools_token1 ON curated_pools(token1)",
    # Which flash-loan venue each route was priced against. Nullable with no default: rows
    # written before this column existed genuinely do not know, and backfilling them with
    # 'aave' would invent a fact -- a shadow comparison must not treat a guess as a sample.
    "ALTER TABLE route_evaluations ADD COLUMN IF NOT EXISTS flash_provider VARCHAR(16)",
    "CREATE INDEX IF NOT EXISTS ix_route_evaluations_flash_provider ON route_evaluations(flash_provider)",
]

async def migrate_v012() -> None:
    async with engine.begin() as conn:
        for statement in _STATEMENTS:
            await conn.execute(text(statement))
