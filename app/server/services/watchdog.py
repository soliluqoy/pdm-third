"""
PREDICT — offline watchdog. Cars that stop sending data (and have no active
issues) fall back to GREY so the dashboard tells the truth.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from server.config import settings
from server.db import async_session_factory
from server.models import Health, Vehicle
from server.services.health import recompute_and_broadcast

logger = logging.getLogger("predict.watchdog")

_task: Optional[asyncio.Task] = None


async def _sweep_once() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.OFFLINE_AFTER_SECONDS)
    async with async_session_factory() as session:
        stale_ids = (await session.execute(
            select(Vehicle.id).where(
                Vehicle.health == Health.GREEN,
                (Vehicle.last_seen.is_(None)) | (Vehicle.last_seen < cutoff),
            )
        )).scalars().all()
        for vid in stale_ids:
            new_health = await recompute_and_broadcast(session, vid, reason="offline_watchdog")
            if new_health:
                await session.commit()
                logger.info("Vehicle %s → %s (telemetry stale)", vid, new_health)


async def _loop() -> None:
    while True:
        try:
            await _sweep_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Watchdog sweep failed: %s", e)
        await asyncio.sleep(settings.WATCHDOG_INTERVAL_SECONDS)


def start() -> None:
    global _task
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
