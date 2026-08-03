"""
PREDICT v3 — alert lifecycle: create/dedup, auto/manual resolve, history.

A firing rule opens ONE alert (dedup by rule+vehicle); a re-fire just bumps
occurrence_count and keeps the latest trigger value — no spam rows. When the
condition clears, the alert auto-resolves. Alerts are never deleted — this
table IS the alert history.

All functions flush but never commit; the caller owns the transaction.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.models import Alert, AlertStatus, Rule, Severity, utcnow

logger = logging.getLogger("predict.alerts")


# ── Creation / dedup ──────────────────────────────────────────────────────────
async def create_or_refresh(
    session: AsyncSession,
    *,
    rule: Optional[Rule],
    vehicle_id: int,
    severity: Severity,
    title: str,
    message: str,
    trigger_value: Optional[float] = None,
    dedup_key: Optional[str] = None,
) -> tuple[Alert, bool]:
    """Open an alert for (rule, vehicle), or refresh the still-active one.

    Returns (alert, created). A re-fire of an already-active alert bumps
    occurrence_count and keeps the latest message/value; once the alert has
    been resolved/dismissed the next fire opens a fresh row.
    """
    key = dedup_key or f"{rule.id if rule else 'adhoc'}:{vehicle_id}"
    existing = (await session.execute(
        select(Alert).where(
            Alert.dedup_key == key,
            Alert.status == AlertStatus.ACTIVE,
        )
    )).scalar_one_or_none()

    if existing is not None:
        existing.occurrence_count += 1
        existing.severity = severity
        existing.title = title
        existing.message = message
        existing.trigger_value = trigger_value
        existing.trigger_timestamp = utcnow()
        await session.flush()
        return existing, False

    alert = Alert(
        vehicle_id=vehicle_id,
        rule_id=rule.id if rule else None,
        severity=severity,
        status=AlertStatus.ACTIVE,
        title=title,
        message=message,
        trigger_value=trigger_value,
        trigger_timestamp=utcnow(),
        dedup_key=key,
        occurrence_count=1,
    )
    session.add(alert)
    await session.flush()
    logger.info("Alert #%d opened [%s]: %s (vehicle %d)",
                alert.id, severity.value, title, vehicle_id)
    return alert, True


# ── History ───────────────────────────────────────────────────────────────────
async def list_alerts(
    session: AsyncSession,
    *,
    vehicle_id: Optional[int] = None,
    status: Optional[str] = None,      # active | resolved | dismissed | None = all
    severity: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[Alert]:
    """Alert history, newest first. Alerts are never deleted."""
    q = select(Alert).order_by(Alert.created_at.desc())
    if vehicle_id is not None:
        q = q.where(Alert.vehicle_id == vehicle_id)
    if status:
        q = q.where(Alert.status == AlertStatus(status))
    if severity:
        q = q.where(Alert.severity == Severity(severity))
    q = q.limit(limit).offset(offset)
    result = await session.execute(q)
    return list(result.scalars().all())


# ── Resolve / dismiss ─────────────────────────────────────────────────────────
async def auto_resolve(
    session: AsyncSession,
    *,
    rule: Rule,
    vehicle_id: int,
) -> Optional[Alert]:
    """The condition cleared (and stayed clear) → close the still-active
    alert for (rule, vehicle). No-op (None) when nothing is active."""
    alert = (await session.execute(
        select(Alert).where(
            Alert.dedup_key == f"{rule.id}:{vehicle_id}",
            Alert.status == AlertStatus.ACTIVE,
        )
    )).scalar_one_or_none()
    if alert is None:
        return None
    alert.status = AlertStatus.RESOLVED
    alert.resolved_at = utcnow()
    await session.flush()
    logger.info("Alert #%d auto-resolved: %s (vehicle %d)",
                alert.id, alert.title, vehicle_id)
    return alert


async def _close(
    session: AsyncSession,
    alert_id: int,
    status: AlertStatus,
) -> Optional[Alert]:
    alert = await session.get(Alert, alert_id)
    if alert is None or alert.status != AlertStatus.ACTIVE:
        return None                      # not found or already closed
    alert.status = status
    alert.resolved_at = utcnow()
    await session.flush()
    return alert


async def resolve(session: AsyncSession, alert_id: int) -> Optional[Alert]:
    """Manual resolve from the Alerts page. None if missing/already closed."""
    return await _close(session, alert_id, AlertStatus.RESOLVED)


async def dismiss(session: AsyncSession, alert_id: int) -> Optional[Alert]:
    """Manual dismiss — stays in history, never re-fires while dismissed."""
    return await _close(session, alert_id, AlertStatus.DISMISSED)


# ── Counts ────────────────────────────────────────────────────────────────────
async def count_active_by_vehicle(session: AsyncSession) -> dict[int, dict[str, int]]:
    """vehicle_id → {severity: n, …, total: n} over ACTIVE alerts
    (feeds the overview cards)."""
    result = await session.execute(
        select(Alert.vehicle_id, Alert.severity, func.count())
        .where(Alert.status == AlertStatus.ACTIVE)
        .group_by(Alert.vehicle_id, Alert.severity)
    )
    out: dict[int, dict[str, int]] = {}
    for vehicle_id, severity, count in result.all():
        counts = out.setdefault(vehicle_id, {"total": 0})
        counts[severity.value] = int(count)
        counts["total"] += int(count)
    return out
