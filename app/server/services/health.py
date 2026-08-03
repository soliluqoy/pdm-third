"""
PREDICT v3 — vehicle health computation.

RED    = an urgent (critical) alert is active, or any component score < 15
YELLOW = a check-soon (warning) alert is active, or any component score < 30
GREEN  = sending data, nothing active
GREY   = offline / never seen

Alert-driven RED is never downgraded by prognostics.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import settings
from server.models import Alert, AlertStatus, Health, HealthEvent, Severity, Vehicle
from server.ws import hub

logger = logging.getLogger("predict.health")

_HEALTH_RANK = {Health.GREY: 0, Health.GREEN: 1, Health.YELLOW: 2, Health.RED: 3}


def _is_stale(last_seen: Optional[datetime]) -> bool:
    if last_seen is None:
        return True
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last_seen > timedelta(
        seconds=settings.OFFLINE_AFTER_SECONDS
    )


def _worse(a: Health, b: Health) -> Health:
    return a if _HEALTH_RANK[a] >= _HEALTH_RANK[b] else b


async def recompute_health(
    session: AsyncSession,
    vehicle_id: int,
    reason: Optional[str] = None,
    prognostics: Optional[dict] = None,
) -> Optional[Health]:
    """Recompute health from active alerts + freshness (+ optional prognostics).
    Returns the new health if it changed (HealthEvent logged), else None."""
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle is None:
        return None

    severities = (await session.execute(
        select(Alert.severity).where(
            Alert.vehicle_id == vehicle_id,
            Alert.status == AlertStatus.ACTIVE,
        )
    )).scalars().all()

    if any(s == Severity.CRITICAL for s in severities):
        new_health = Health.RED
    elif any(s == Severity.WARNING for s in severities):
        new_health = Health.YELLOW
    elif not _is_stale(vehicle.last_seen):
        new_health = Health.GREEN
    else:
        new_health = Health.GREY

    if prognostics:
        scores = [
            float(v) for v in prognostics.values()
            if isinstance(v, (int, float))
        ]
        if scores:
            worst = min(scores)
            if worst < 15:
                new_health = _worse(new_health, Health.RED)
            elif worst < 30:
                new_health = _worse(new_health, Health.YELLOW)

    if vehicle.health != new_health:
        session.add(HealthEvent(
            vehicle_id=vehicle_id,
            from_health=vehicle.health,
            to_health=new_health,
            reason=reason or "recompute",
            timestamp=datetime.now(timezone.utc),
        ))
        vehicle.health = new_health
        return new_health
    return None


async def recompute_and_broadcast(
    session: AsyncSession,
    vehicle_id: int,
    reason: Optional[str] = None,
    prognostics: Optional[dict] = None,
) -> Optional[str]:
    new_health = await recompute_health(
        session, vehicle_id, reason=reason, prognostics=prognostics,
    )
    if new_health:
        await hub.broadcast("health", {
            "vehicle_id": vehicle_id, "health": new_health.value,
        })
    return new_health.value if new_health else None
