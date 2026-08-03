"""PREDICT v3 — alerts: active list, full history, resolve/dismiss.

Alerts are never deleted — this table IS the alert history.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server import alerts as alerts_service
from server.api.deps import SessionDep
from server.models import Alert, AlertStatus, Vehicle
from server.services.health import recompute_and_broadcast
from server.ws import hub

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _alert_dict(a: Alert, vehicle_name: Optional[str]) -> dict:
    return {
        "id": a.id, "vehicle_id": a.vehicle_id, "vehicle_name": vehicle_name,
        "rule_id": a.rule_id, "severity": a.severity.value, "status": a.status.value,
        "title": a.title, "message": a.message,
        "trigger_value": a.trigger_value,
        "trigger_timestamp": a.trigger_timestamp.isoformat() if a.trigger_timestamp else None,
        "occurrence_count": a.occurrence_count,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
        "work_order_id": a.work_order_id,
    }


@router.get("")
async def list_alerts(
    status: str = Query(default="active"),   # active | resolved | dismissed | all
    vehicle_id: Optional[int] = None,
    severity: Optional[str] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = SessionDep,
):
    rows = await alerts_service.list_alerts(
        session, vehicle_id=vehicle_id,
        status=None if status == "all" else status,
        severity=severity, limit=limit, offset=offset,
    )
    names = {v.id: v.name for v in (await session.execute(select(Vehicle))).scalars().all()}
    return [_alert_dict(a, names.get(a.vehicle_id)) for a in rows]


async def _transition(session: AsyncSession, alert_id: int, action: str) -> dict:
    fn = alerts_service.resolve if action == "resolve" else alerts_service.dismiss
    a = await fn(session, alert_id)
    if a is None:
        raise HTTPException(409, "Alert not found or already closed")
    await recompute_and_broadcast(session, a.vehicle_id, reason=f"alert_{action}")
    await session.commit()
    name = (await session.execute(
        select(Vehicle.name).where(Vehicle.id == a.vehicle_id)
    )).scalar_one_or_none()
    payload = _alert_dict(a, name)
    await hub.broadcast("alert", payload)
    return payload


@router.post("/{alert_id}/resolve")
async def resolve_alert(alert_id: int, session: AsyncSession = SessionDep):
    return await _transition(session, alert_id, "resolve")


@router.post("/{alert_id}/dismiss")
async def dismiss_alert(alert_id: int, session: AsyncSession = SessionDep):
    return await _transition(session, alert_id, "dismiss")