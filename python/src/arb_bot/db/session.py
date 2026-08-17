from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from .models import Base
from ..config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("ALTER TABLE route_evaluations ADD COLUMN IF NOT EXISTS evaluation_latency_ms INTEGER"))
        await conn.execute(text("ALTER TABLE route_evaluations ADD COLUMN IF NOT EXISTS provider_disagreement BOOLEAN"))
        await conn.execute(text("ALTER TABLE route_evaluations ADD COLUMN IF NOT EXISTS would_submit BOOLEAN NOT NULL DEFAULT false"))
