from sqlalchemy import text
from .session import engine

_STATEMENTS = [
    "ALTER TABLE pending_pool_events ADD COLUMN IF NOT EXISTS source_sender VARCHAR(42)",
    "ALTER TABLE pending_pool_events ADD COLUMN IF NOT EXISTS tx_touched_pools_json TEXT NOT NULL DEFAULT '[]'",
    "CREATE INDEX IF NOT EXISTS ix_pending_pool_events_source_sender ON pending_pool_events(source_sender)",
    "ALTER TABLE verified_pools ADD COLUMN IF NOT EXISTS tick_spacing INTEGER",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS route_family VARCHAR(66)",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS survival_probability_bps INTEGER",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS route_capture_probability_bps INTEGER",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS shadow_action VARCHAR(12)",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS shadow_confidence_bps INTEGER",
    "ALTER TABLE opportunity_telemetry ADD COLUMN IF NOT EXISTS shadow_features_json TEXT NOT NULL DEFAULT '{}'",
    "CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_route_family ON opportunity_telemetry(route_family)",
    "CREATE INDEX IF NOT EXISTS ix_opportunity_telemetry_shadow_action ON opportunity_telemetry(shadow_action)",
]

async def migrate_v012() -> None:
    async with engine.begin() as conn:
        for statement in _STATEMENTS:
            await conn.execute(text(statement))
