"""
PREDICT v3 — work orders (maintenance to-dos) + maintenance history.

Board:   SUGGESTED | OPEN | IN_PROGRESS
Done:    → writes immutable maintenance_log (history), auto-resolves linked alert.
History: maintenance_log is append-only, filterable, CSV-exportable.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server import workorders as wo_service
from server.api.deps import SessionDep
from server.models import Alert, Vehicle, WorkOrder
from server.rules import reanchor_scheduled
from server.schemas import WorkOrderComplete, WorkOrderCreate
from server.ws import hub

router = APIRouter(tags=["work orders", "maintenance"])


def _wo_dict(t: WorkOrder, vehicle_name: Optional[str]) -> dict:
    return {
        "id": t.id, "vehicle_id": t.vehicle_id, "vehicle_name": vehicle_name,
        "alert_id": t.alert_id, "title": t.title, "description": t.description,
        "priority": t.priority.value, "status": t.status.value, "source": t.source.value,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "due_date": t.due_date.isoformat() if t.due_date else None,
        "started_at": t.started_at.isoformat() if t.started_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "completion_notes": t.completion_notes,
        "cost": float(t.cost) if t.cost is not None else None,
        "odometer_at_completion": t.odometer_at_completion,
    }


async def _broadcast(session: AsyncSession, t: WorkOrder) -> dict:
    name = (await session.execute(
        select(Vehicle.name).where(Vehicle.id == t.vehicle_id)
    )).scalar_one_or_none()
    payload = _wo_dict(t, name)
    await hub.broadcast("work_order", payload)
    return payload


# ── Board ─────────────────────────────────────────────────────────────────────
@router.get("/workorders")
async def list_work_orders(
    status: str = Query(default="board"),   # board | suggested|open|in_progress|done|cancelled|all
    vehicle_id: Optional[int] = None,
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = SessionDep,
):
    q = (
        select(WorkOrder, Vehicle.name)
        .join(Vehicle, Vehicle.id == WorkOrder.vehicle_id)
        .order_by(WorkOrder.created_at.desc())
        .limit(limit)
    )
    if status == "board":
        from server.models import WorkOrderStatus as S
        q = q.where(WorkOrder.status.in_([S.SUGGESTED, S.OPEN, S.IN_PROGRESS]))
    elif status != "all":
        from server.models import WorkOrderStatus as S
        try:
            q = q.where(WorkOrder.status == S(status))
        except ValueError:
            raise HTTPException(400, f"Unknown status {status!r}")
    if vehicle_id:
        q = q.where(WorkOrder.vehicle_id == vehicle_id)
    rows = (await session.execute(q)).all()
    return [_wo_dict(t, name) for t, name in rows]


@router.post("/workorders")
async def create_work_order(body: WorkOrderCreate, session: AsyncSession = SessionDep):
    if await session.get(Vehicle, body.vehicle_id) is None:
        raise HTTPException(404, "Car not found")
    from server.models import WorkOrderPriority
    try:
        priority = WorkOrderPriority(body.priority)
    except ValueError:
        raise HTTPException(400, f"Unknown priority {body.priority!r}")
    t = await wo_service.create_manual(
        session, vehicle_id=body.vehicle_id, title=body.title,
        description=body.description, priority=priority, due_date=body.due_date,
    )
    await session.commit()
    return await _broadcast(session, t)


async def _get_or_404(session: AsyncSession, wo_id: int) -> WorkOrder:
    t = await session.get(WorkOrder, wo_id)
    if t is None:
        raise HTTPException(404, "Work order not found")
    return t


@router.post("/workorders/{wo_id}/approve")
async def approve_work_order(wo_id: int, session: AsyncSession = SessionDep):
    await _get_or_404(session, wo_id)
    t = await wo_service.approve(session, wo_id)
    if t is None:
        raise HTTPException(409, "Work order is not in suggested state")
    await session.commit()
    return await _broadcast(session, t)


@router.post("/workorders/{wo_id}/start")
async def start_work_order(wo_id: int, session: AsyncSession = SessionDep):
    await _get_or_404(session, wo_id)
    t = await wo_service.start(session, wo_id)
    if t is None:
        raise HTTPException(409, "Work order is not open")
    await session.commit()
    return await _broadcast(session, t)


@router.post("/workorders/{wo_id}/complete")
async def complete_work_order(wo_id: int, body: WorkOrderComplete,
                              session: AsyncSession = SessionDep):
    await _get_or_404(session, wo_id)
    t = await wo_service.complete(
        session, wo_id,
        completion_notes=body.notes, cost=body.cost, odometer=body.odometer,
    )
    if t is None:
        raise HTTPException(409, "Work order is already closed")
    # Re-anchor a scheduled rule so the next interval counts from this completion.
    if t.alert_id:
        alert = await session.get(Alert, t.alert_id)
        if alert and alert.rule_id:
            await reanchor_scheduled(session, alert.rule_id, t.vehicle_id)
    await session.commit()
    return await _broadcast(session, t)


@router.post("/workorders/{wo_id}/cancel")
async def cancel_work_order(wo_id: int, session: AsyncSession = SessionDep):
    await _get_or_404(session, wo_id)
    t = await wo_service.cancel(session, wo_id)
    if t is None:
        raise HTTPException(409, "Work order is already closed")
    await session.commit()
    return await _broadcast(session, t)


# ── Maintenance history ───────────────────────────────────────────────────────
@router.get("/maintenance")
async def maintenance_history(
    vehicle_id: Optional[int] = None,
    limit: int = Query(default=500, ge=1, le=2000),
    session: AsyncSession = SessionDep,
):
    rows = await wo_service.maintenance_history(
        session, vehicle_id=vehicle_id, limit=limit)
    names = {v.id: v.name for v in (await session.execute(select(Vehicle))).scalars().all()}
    return [
        {
            "id": m.id, "vehicle_id": m.vehicle_id,
            "vehicle_name": names.get(m.vehicle_id),
            "work_order_id": m.work_order_id, "event_type": m.event_type,
            "title": m.title, "notes": m.notes,
            "cost": float(m.cost) if m.cost is not None else None,
            "odometer": m.odometer,
            "event_date": m.event_date.isoformat() if m.event_date else None,
        }
        for m in rows
    ]


@router.get("/maintenance/export.csv", response_class=PlainTextResponse)
async def maintenance_csv(vehicle_id: Optional[int] = None,
                          session: AsyncSession = SessionDep):
    csv_text = await wo_service.maintenance_csv(session, vehicle_id=vehicle_id)
    return PlainTextResponse(
        csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=maintenance_history.csv"},
    )