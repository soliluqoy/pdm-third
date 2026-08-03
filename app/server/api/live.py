"""PREDICT v3 — live overview + header summary (single round-trips)."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server import alerts as alerts_service
from server import workorders as wo_service
from server.api.deps import SessionDep
from server.config import settings
from server.models import (
    Alert,
    AlertStatus,
    ComponentHealth,
    Severity,
    Vehicle,
    WorkOrder,
    WorkOrderStatus,
)
from server.state import live_store
from server.services.predictor import compact_prognostics

router = APIRouter(tags=["live"])


@router.get("/overview")
async def overview(session: AsyncSession = SessionDep):
    """First-glance payload: every car's card in one request."""
    vehicles = list((await session.execute(
        select(Vehicle).order_by(Vehicle.name)
    )).scalars().all())

    alerts_by_vehicle = await alerts_service.count_active_by_vehicle(session)
    wo_by_vehicle = await wo_service.count_open_by_vehicle(session)
    prog_rows = (await session.execute(select(ComponentHealth))).scalars().all()
    prog = {r.vehicle_id: r for r in prog_rows}

    return [
        {
            "id": v.id, "name": v.name, "license_plate": v.license_plate,
            "device_type": v.device_type, "health": v.health.value,
            "last_seen": v.last_seen.isoformat() if v.last_seen else None,
            "live": live_store.get(v.id).to_dict(settings.OFFLINE_AFTER_SECONDS),
            "alerts": alerts_by_vehicle.get(v.id, {}),
            "open_work_orders": wo_by_vehicle.get(v.id, 0),
            "prognostics": compact_prognostics(prog.get(v.id)),
        }
        for v in vehicles
    ]


@router.get("/live/fleet")
async def fleet_live(session: AsyncSession = SessionDep):
    """Back-compat alias of /overview."""
    return await overview(session)


@router.get("/live/summary")
async def summary(session: AsyncSession = SessionDep):
    """Header badges: cars by health, active alerts by severity, open WOs."""
    health_rows = await session.execute(
        select(Vehicle.health, func.count()).group_by(Vehicle.health)
    )
    alert_rows = await session.execute(
        select(Alert.severity, func.count())
        .where(Alert.status == AlertStatus.ACTIVE)
        .group_by(Alert.severity)
    )
    wo_rows = await session.execute(
        select(WorkOrder.status, func.count())
        .where(WorkOrder.status.in_([
            WorkOrderStatus.SUGGESTED, WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS,
        ]))
        .group_by(WorkOrder.status)
    )
    return {
        "cars": {h.value: int(c) for h, c in health_rows.all()},
        "cars_total": sum(int(c) for _, c in health_rows.all()),
        "alerts": {s.value: int(c) for s, c in alert_rows.all()},
        "alerts_total": sum(int(c) for _, c in alert_rows.all()),
        "urgent": sum(int(c) for s, c in alert_rows.all() if s == Severity.CRITICAL),
        "work_orders": {s.value: int(c) for s, c in wo_rows.all()},
        "suggested": sum(int(c) for s, c in wo_rows.all()
                        if s == WorkOrderStatus.SUGGESTED),
    }