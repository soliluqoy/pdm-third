"""
PREDICT v3 — work order lifecycle + maintenance history.

suggested → open → in_progress → done        (done writes MaintenanceLog)
    └──────────────→ cancelled (dismissed suggestion / cancelled planned work)

All transitions go through this module so the maintenance_log write and the
linked-alert auto-resolve can never be forgotten.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import (
    Alert,
    AlertStatus,
    FailureEvent,
    MaintenanceLog,
    Vehicle,
    WorkOrder,
    WorkOrderPriority,
    WorkOrderSource,
    WorkOrderStatus,
)

logger = logging.getLogger("predict.workorders")


# ── Creation ──────────────────────────────────────────────────────────────────
async def draft_from_rule(
    session: AsyncSession,
    *,
    vehicle_id: int,
    alert: Alert,
    title: str,
    description: Optional[str],
    priority: WorkOrderPriority,
    shadow_mode: bool = True,
) -> WorkOrder:
    """A firing rule with a recommendation drafts a work order.
    shadow_mode ON → SUGGESTED (awaiting review); OFF → straight to OPEN."""
    wo = WorkOrder(
        vehicle_id=vehicle_id,
        alert_id=alert.id,
        title=title,
        description=description,
        priority=priority,
        status=WorkOrderStatus.SUGGESTED if shadow_mode else WorkOrderStatus.OPEN,
        source=WorkOrderSource.AUTO,
    )
    session.add(wo)
    await session.flush()
    alert.work_order_id = wo.id
    logger.info("WorkOrder #%d drafted (%s): %s", wo.id, wo.status.value, title)
    return wo


async def create_manual(
    session: AsyncSession,
    *,
    vehicle_id: int,
    title: str,
    description: Optional[str] = None,
    priority: WorkOrderPriority = WorkOrderPriority.MEDIUM,
    due_date: Optional[datetime] = None,
) -> WorkOrder:
    wo = WorkOrder(
        vehicle_id=vehicle_id,
        title=title,
        description=description,
        priority=priority,
        status=WorkOrderStatus.OPEN,
        source=WorkOrderSource.MANUAL,
        due_date=due_date,
    )
    session.add(wo)
    await session.flush()
    return wo


# ── Transitions ───────────────────────────────────────────────────────────────
async def approve(session: AsyncSession, wo_id: int) -> Optional[WorkOrder]:
    wo = await session.get(WorkOrder, wo_id)
    if wo is None or wo.status != WorkOrderStatus.SUGGESTED:
        return None
    wo.status = WorkOrderStatus.OPEN
    return wo


async def start(session: AsyncSession, wo_id: int) -> Optional[WorkOrder]:
    wo = await session.get(WorkOrder, wo_id)
    if wo is None or wo.status != WorkOrderStatus.OPEN:
        return None
    wo.status = WorkOrderStatus.IN_PROGRESS
    wo.started_at = datetime.now(timezone.utc)
    return wo


async def complete(
    session: AsyncSession,
    wo_id: int,
    *,
    completion_notes: Optional[str] = None,
    cost: Optional[float] = None,
    odometer: Optional[float] = None,
    failure_class: Optional[str] = None,
    failure_component: Optional[str] = None,
    failure_symptom: Optional[str] = None,
) -> Optional[WorkOrder]:
    """→ DONE and write the immutable maintenance_log row.
    Also auto-resolves the linked alert, if any.

    failure_class: "preventive" (done before failure) | "reactive" (the
    component actually failed). A reactive completion also writes a
    FailureEvent — the ground-truth label for failure-prediction models.
    """
    wo = await session.get(WorkOrder, wo_id)
    if wo is None or wo.status in (WorkOrderStatus.DONE, WorkOrderStatus.CANCELLED):
        return None

    now = datetime.now(timezone.utc)
    wo.status = WorkOrderStatus.DONE
    wo.completed_at = now
    wo.completion_notes = completion_notes
    wo.cost = cost
    wo.odometer_at_completion = odometer

    session.add(MaintenanceLog(
        vehicle_id=wo.vehicle_id,
        work_order_id=wo.id,
        event_type="repair",
        title=wo.title,
        notes=completion_notes or wo.description,
        cost=cost,
        odometer=odometer,
        failure_class=failure_class,
        event_date=now,
    ))

    # Ground-truth label: a reactive completion means the component failed.
    if failure_class == "reactive":
        component = failure_component or "other"
        session.add(FailureEvent(
            vehicle_id=wo.vehicle_id,
            work_order_id=wo.id,
            component=component,
            symptom=failure_symptom or completion_notes or wo.title,
            odometer=odometer,
            occurred_at=now,
        ))
        # Label daily feature rows in the lookback window for ML evaluation.
        from server.services import models as models_service
        labeled = await models_service.label_failure_features(
            session,
            vehicle_id=wo.vehicle_id,
            component=component,
            occurred_at=now,
        )
        logger.info(
            "FailureEvent recorded: vehicle %d component=%s (labeled %d feature rows)",
            wo.vehicle_id, component, labeled,
        )

    if wo.alert_id:
        alert = await session.get(Alert, wo.alert_id)
        if alert is not None and alert.status == AlertStatus.ACTIVE:
            alert.status = AlertStatus.RESOLVED
            alert.resolved_at = now
        # Reset predictive component wear when a predict_* work order completes
        if alert is not None and alert.rule_id:
            from server.models import Rule
            from server.services import predictor
            rule = await session.get(Rule, alert.rule_id)
            component = predictor.component_for_rule_key(rule.key if rule else None)
            if component:
                await predictor.reset_component(
                    session, wo.vehicle_id, component, odometer=odometer,
                )

    logger.info("WorkOrder #%d completed → maintenance_log", wo.id)
    return wo


async def cancel(session: AsyncSession, wo_id: int) -> Optional[WorkOrder]:
    wo = await session.get(WorkOrder, wo_id)
    if wo is None or wo.status in (WorkOrderStatus.DONE, WorkOrderStatus.CANCELLED):
        return None
    wo.status = WorkOrderStatus.CANCELLED
    return wo


# ── Queries ───────────────────────────────────────────────────────────────────
async def list_work_orders(
    session: AsyncSession,
    *,
    vehicle_id: Optional[int] = None,
    status: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[WorkOrder]:
    q = select(WorkOrder).order_by(WorkOrder.created_at.desc())
    if vehicle_id is not None:
        q = q.where(WorkOrder.vehicle_id == vehicle_id)
    if status:
        q = q.where(WorkOrder.status == WorkOrderStatus(status))
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return list(result.scalars().all())


async def count_open_by_vehicle(session: AsyncSession) -> dict[int, int]:
    """Open + in_progress + suggested per vehicle (for overview cards)."""
    result = await session.execute(
        select(WorkOrder.vehicle_id, func.count())
        .where(WorkOrder.status.in_([
            WorkOrderStatus.SUGGESTED, WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS,
        ]))
        .group_by(WorkOrder.vehicle_id)
    )
    return {vid: count for vid, count in result.all()}


async def maintenance_history(
    session: AsyncSession,
    *,
    vehicle_id: Optional[int] = None,
    limit: int = 500,
    offset: int = 0,
) -> list[MaintenanceLog]:
    q = select(MaintenanceLog).order_by(MaintenanceLog.event_date.desc())
    if vehicle_id is not None:
        q = q.where(MaintenanceLog.vehicle_id == vehicle_id)
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return list(result.scalars().all())


async def maintenance_csv(session: AsyncSession, vehicle_id: Optional[int] = None) -> str:
    rows = await maintenance_history(session, vehicle_id=vehicle_id, limit=10000)
    vehicles = {v.id: v.name for v in (await session.execute(select(Vehicle))).scalars().all()}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "vehicle", "type", "title", "notes", "cost", "odometer", "failure_class"])
    for r in rows:
        w.writerow([
            r.event_date.isoformat(), vehicles.get(r.vehicle_id, r.vehicle_id),
            r.event_type, r.title, (r.notes or "").replace("\n", " "),
            r.cost if r.cost is not None else "", r.odometer if r.odometer is not None else "",
            r.failure_class or "",
        ])
    return buf.getvalue()
