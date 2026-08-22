from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select

from ..event_graph import PoolMetadata
from .models import DiscoveryCheckpointRecord, VerifiedPoolRecord
from .session import SessionLocal


class PostgresPoolRegistry:
    """Durable verified-pool registry and factory scan checkpoints.

    Pools and the corresponding next cursor are committed atomically. A failed
    transaction therefore cannot advance discovery past data that was not saved.
    """

    async def load_pools(self) -> list[PoolMetadata]:
        async with SessionLocal() as db:
            rows = (await db.execute(select(VerifiedPoolRecord))).scalars().all()
            return [
                PoolMetadata.create(
                    address=row.address,
                    token0=row.token0,
                    token1=row.token1,
                    venue=row.venue,
                    pool_type=row.pool_type,
                    fee=row.fee,
                    stable=row.stable,
                    tick_spacing=row.tick_spacing,
                )
                for row in rows
            ]

    async def load_cursor(self, source: str, factory: str, default: int) -> int:
        async with SessionLocal() as db:
            row = await db.get(DiscoveryCheckpointRecord, source)
            if row is None or row.factory.lower() != factory.lower():
                return int(default)
            return max(int(default), int(row.next_cursor))

    async def save_batch(
        self,
        *,
        source: str,
        factory: str,
        next_cursor: int,
        pools: Iterable[PoolMetadata],
        verified_block: int | None = None,
    ) -> None:
        async with SessionLocal() as db:
            for pool in pools:
                await db.merge(
                    VerifiedPoolRecord(
                        address=pool.address,
                        source=source,
                        factory=factory,
                        token0=pool.token0,
                        token1=pool.token1,
                        venue=pool.venue,
                        pool_type=pool.pool_type,
                        fee=pool.fee,
                        stable=pool.stable,
                        tick_spacing=pool.tick_spacing,
                        last_verified_block=verified_block,
                    )
                )
            await db.merge(
                DiscoveryCheckpointRecord(
                    source=source,
                    factory=factory,
                    next_cursor=int(next_cursor),
                    last_scanned_block=verified_block,
                )
            )
            await db.commit()
