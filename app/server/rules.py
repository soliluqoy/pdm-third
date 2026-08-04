"""
PREDICT v3 — rule engine + owner-friendly presets.

Rule types:
  THRESHOLD  sensor vs limit, optional sustained duration (+ auto-resolve)
  DTC        diagnostic trouble code match (empty dtc_code = any code)
  SCHEDULED  odometer / engine-hours / days interval (next-due in settings)
  BEHAVIOR   daily driving-event count
  ANOMALY    created/fired by the baselines service

Firing → alerts.create_or_refresh (dedup by rule+vehicle: a still-active alert
re-fires as occurrence_count += 1, never a spam row). When the condition
clears for the same sustained duration, the alert auto-resolves.
If the rule has auto_work_order + recommendation, a work order is drafted:
  ask_me_first ON  (default) → SUGGESTED for review
  ask_me_first OFF           → straight to OPEN

Performance: active rules cached in-process (invalidated on change), sustained
timers in-process with self-expiry, caller passes its DB session so the hot
path opens zero extra connections.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server import alerts, settings_store, workorders
from server.models import (
    DrivingEvent,
    DrivingEventType,
    Rule,
    RuleType,
    Severity,
    WorkOrderPriority,
)
from server.services.health import recompute_and_broadcast
from server.ws import hub

logger = logging.getLogger("predict.rules")

EQUALITY_EPSILON = 1e-6
OPERATORS = {
    ">": lambda v, t: v > t,
    "<": lambda v, t: v < t,
    ">=": lambda v, t: v >= t,
    "<=": lambda v, t: v <= t,
    "==": lambda v, t: abs(v - t) <= EQUALITY_EPSILON,
}
SCHEDULED_SENSOR_TYPES = ("odometer", "engine_hours")
BEHAVIOR_EVENT_TYPES = tuple(e.value for e in DrivingEventType)


# ── In-process rule cache ─────────────────────────────────────────────────────
class _RuleCache:
    def __init__(self) -> None:
        self._rules: list[Rule] = []
        self._loaded_at = 0.0
        self._dirty = True

    def invalidate(self) -> None:
        self._dirty = True

    async def get(self, session: AsyncSession) -> list[Rule]:
        if self._dirty or (time.monotonic() - self._loaded_at) > 30:
            result = await session.execute(
                select(Rule).where(Rule.is_active == True)  # noqa: E712
            )
            self._rules = list(result.scalars().all())
            self._loaded_at = time.monotonic()
            self._dirty = False
        return self._rules


_rule_cache = _RuleCache()


def invalidate_rules_cache() -> None:
    _rule_cache.invalidate()


# ── Sustained-duration timers (in-process, self-expiring) ────────────────────
_duration_since: dict[tuple[int, int], datetime] = {}
_clear_since: dict[tuple[int, int], datetime] = {}


def _duration_ok(rule_id: int, vehicle_id: int, ts: datetime, seconds: int) -> bool:
    if seconds <= 0:
        return True
    key = (rule_id, vehicle_id)
    since = _duration_since.get(key)
    if since is None:
        _duration_since[key] = ts
        return False
    return (ts - since).total_seconds() >= seconds


def _duration_reset(rule_id: int, vehicle_id: int) -> None:
    _duration_since.pop((rule_id, vehicle_id), None)


def _clear_ok(rule_id: int, vehicle_id: int, ts: datetime, seconds: int) -> bool:
    """Auto-resolve only after the condition has stayed clear for the same
    duration (0 s = resolve on first clear)."""
    if seconds <= 0:
        return True
    key = (rule_id, vehicle_id)
    since = _clear_since.get(key)
    if since is None:
        _clear_since[key] = ts
        return False
    return (ts - since).total_seconds() >= seconds


def _clear_reset(rule_id: int, vehicle_id: int) -> None:
    _clear_since.pop((rule_id, vehicle_id), None)


def _timers_gc() -> None:
    """Drop timers idle for over an hour (car stopped reporting mid-condition)."""
    if len(_duration_since) + len(_clear_since) < 512:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    for store in (_duration_since, _clear_since):
        for key, since in list(store.items()):
            if since < cutoff:
                store.pop(key, None)


# ── Alert + work-order creation ───────────────────────────────────────────────
async def _fire(
    session: AsyncSession,
    *,
    vehicle_id: int,
    vehicle_name: str,
    rule: Rule,
    title: str,
    message: str,
    trigger_value: Optional[float],
    severity: Optional[Severity] = None,
) -> None:
    alert, created = await alerts.create_or_refresh(
        session,
        rule=rule,
        vehicle_id=vehicle_id,
        severity=severity or rule.severity,
        title=title,
        message=message,
        trigger_value=trigger_value,
    )

    wo = None
    if created and rule.auto_work_order and rule.recommendation:
        shadow = await settings_store.get_bool("ask_me_first", True)
        wo = await workorders.draft_from_rule(
            session,
            vehicle_id=vehicle_id,
            alert=alert,
            title=rule.name,
            description=rule.recommendation,
            priority=rule.priority or WorkOrderPriority.MEDIUM,
            shadow_mode=shadow,
        )

    await recompute_and_broadcast(session, vehicle_id, reason=f"alert:{rule.key}")
    await session.commit()

    if created:
        await hub.broadcast("alert", {
            "id": alert.id, "vehicle_id": vehicle_id, "vehicle_name": vehicle_name,
            "severity": alert.severity.value, "status": alert.status.value,
            "title": title, "message": message, "trigger_value": trigger_value,
            "work_order_id": wo.id if wo else None,
            "created_at": alert.created_at.isoformat(),
        })
        if wo:
            await hub.broadcast("work_order", {
                "id": wo.id, "vehicle_id": vehicle_id, "vehicle_name": vehicle_name,
                "title": wo.title, "status": wo.status.value,
                "priority": wo.priority.value, "alert_id": alert.id,
            })
        logger.info("Alert fired: %s → %s (%s)", vehicle_name, title, rule.severity.value)


async def fire_rule(
    session: AsyncSession,
    *,
    vehicle_id: int,
    vehicle_name: str,
    rule: Rule,
    title: str,
    message: str,
    trigger_value: Optional[float] = None,
    severity: Optional[Severity] = None,
) -> None:
    """Public entry for out-of-band detections (baselines/anomaly/PME)."""
    await _fire(
        session,
        vehicle_id=vehicle_id,
        vehicle_name=vehicle_name,
        rule=rule,
        title=title,
        message=message,
        trigger_value=trigger_value,
        severity=severity,
    )


def _applies(rule: Rule, vehicle_id: int) -> bool:
    return rule.vehicle_id is None or rule.vehicle_id == vehicle_id


# ── Threshold + scheduled evaluation (per telemetry record) ──────────────────
async def evaluate_telemetry(
    session: AsyncSession,
    *,
    vehicle_id: int,
    vehicle_name: str,
    sensors: dict[str, float],
    units: dict[str, str],
    ts: datetime,
) -> None:
    _timers_gc()
    rules = await _rule_cache.get(session)
    for rule in rules:
        if not _applies(rule, vehicle_id):
            continue
        if rule.rule_type == RuleType.THRESHOLD:
            await _eval_threshold(session, rule, vehicle_id, vehicle_name, sensors, units, ts)
        elif rule.rule_type == RuleType.SCHEDULED:
            await _eval_scheduled(session, rule, vehicle_id, vehicle_name, sensors, ts)


async def _eval_threshold(session, rule, vehicle_id, vehicle_name, sensors, units, ts):
    if not rule.sensor_type or rule.threshold_value is None or not rule.operator:
        return
    if rule.sensor_type not in sensors:
        # Sparse records are normal: absence = "no information", keep any
        # running duration window instead of resetting it.
        return
    value = sensors[rule.sensor_type]

    if not OPERATORS[rule.operator](value, rule.threshold_value):
        # Condition not met → cancel any arming, and auto-resolve an active
        # alert once the clear period (same duration) has elapsed.
        _duration_reset(rule.id, vehicle_id)
        if _clear_ok(rule.id, vehicle_id, ts, rule.duration_seconds or 0):
            resolved = await alerts.auto_resolve(session, rule=rule, vehicle_id=vehicle_id)
            if resolved is not None:
                await recompute_and_broadcast(session, vehicle_id,
                                              reason=f"auto-resolve:{rule.key}")
                await session.commit()
                await hub.broadcast("alert_resolved", {
                    "id": resolved.id, "vehicle_id": vehicle_id,
                    "title": resolved.title, "auto": True,
                })
                _clear_reset(rule.id, vehicle_id)
        return

    # Condition met → cancel any pending auto-resolve, arm/continue duration.
    _clear_reset(rule.id, vehicle_id)
    if not _duration_ok(rule.id, vehicle_id, ts, rule.duration_seconds or 0):
        return

    unit = units.get(rule.sensor_type, "")
    label = rule.sensor_type.replace("_", " ")
    await _fire(
        session,
        vehicle_id=vehicle_id,
        vehicle_name=vehicle_name,
        rule=rule,
        title=rule.name,
        message=f"{label.capitalize()} is {value:g}{unit} "
                f"(limit: {rule.operator} {rule.threshold_value:g}{unit})",
        trigger_value=value,
    )
    _duration_reset(rule.id, vehicle_id)


async def _eval_scheduled(session, rule, vehicle_id, vehicle_name, sensors, ts):
    """Interval maintenance on a monotonic counter (odometer / engine hours).
    Next-due is persisted in settings so restarts don't lose the anchor."""
    if (not rule.interval_value or rule.interval_value <= 0
            or rule.sensor_type not in SCHEDULED_SENSOR_TYPES):
        return
    value = sensors.get(rule.sensor_type)
    if value is None:
        return

    key = f"rule.{rule.id}.vehicle.{vehicle_id}.next_due"
    raw = await settings_store.get(key)
    if raw is None:
        await settings_store.set_value(key, str(value + rule.interval_value))
        return
    try:
        next_due = float(raw)
    except (TypeError, ValueError):
        next_due = value + rule.interval_value

    if value < next_due:
        return

    unit = "km" if rule.sensor_type == "odometer" else "h"
    await _fire(
        session,
        vehicle_id=vehicle_id,
        vehicle_name=vehicle_name,
        rule=rule,
        title=rule.name,
        message=f"Odometer reached {value:,.0f} {unit} — "
                f"scheduled every {rule.interval_value:,.0f} {unit}.",
        trigger_value=value,
    )
    await settings_store.set_value(key, str(value + rule.interval_value))


async def reanchor_scheduled(session: AsyncSession, rule_id: int, vehicle_id: int) -> None:
    """Called when a scheduled work order is completed: next due = current + interval."""
    rule = await session.get(Rule, rule_id)
    if rule is None or not rule.interval_value:
        return
    key = f"rule.{rule_id}.vehicle.{vehicle_id}.next_due"
    raw = await settings_store.get(key)
    try:
        await settings_store.set_value(key, str(float(raw) + rule.interval_value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass


# ── DTC evaluation ────────────────────────────────────────────────────────────
async def evaluate_dtc(
    session: AsyncSession,
    *,
    vehicle_id: int,
    vehicle_name: str,
    dtc_code: str,
    ts: datetime,
) -> None:
    if not dtc_code:
        return
    rules = [
        r for r in await _rule_cache.get(session)
        if r.rule_type == RuleType.DTC
        and (not r.dtc_code or r.dtc_code == dtc_code)
        and _applies(r, vehicle_id)
    ]
    for rule in rules:
        await _fire(
            session,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            rule=rule,
            title=f"Fault code {dtc_code}",
            message=f"The car reported diagnostic trouble code {dtc_code}.",
            trigger_value=None,
        )


# ── Behavior evaluation (daily event counts) ─────────────────────────────────
async def evaluate_behavior(
    session: AsyncSession,
    *,
    vehicle_id: int,
    vehicle_name: str,
    ts: Optional[datetime] = None,
) -> None:
    ts = ts or datetime.now(timezone.utc)
    day_start = datetime(ts.year, ts.month, ts.day, tzinfo=timezone.utc)
    day_end = day_start + timedelta(days=1)

    for rule in await _rule_cache.get(session):
        if rule.rule_type != RuleType.BEHAVIOR or not _applies(rule, vehicle_id):
            continue
        if (not rule.sensor_type or rule.sensor_type not in BEHAVIOR_EVENT_TYPES
                or rule.threshold_value is None):
            continue
        count = (await session.execute(
            select(func.count()).select_from(DrivingEvent).where(
                DrivingEvent.vehicle_id == vehicle_id,
                DrivingEvent.event_type == DrivingEventType(rule.sensor_type),
                DrivingEvent.ts >= day_start,
                DrivingEvent.ts < day_end,
            )
        )).scalar() or 0

        op = rule.operator or ">="
        if not OPERATORS[op](float(count), float(rule.threshold_value)):
            continue
        label = rule.sensor_type.replace("_", " ")
        await _fire(
            session,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            rule=rule,
            title=rule.name,
            message=f"{count} {label} events today "
                    f"(limit: {op} {rule.threshold_value:g})",
            trigger_value=float(count),
        )


# ── Presets (seeded at startup, upsert by key) ────────────────────────────────
PRESETS: list[dict] = [
    {
        "key": "overheat", "rule_type": RuleType.THRESHOLD,
        "name": "Engine overheating",
        "description": "Coolant temperature critically high",
        "sensor_type": "coolant_temperature", "operator": ">",
        "threshold_value": 110, "duration_seconds": 120,
        "severity": Severity.CRITICAL, "priority": WorkOrderPriority.URGENT,
        "auto_work_order": True,
        "recommendation": "Stop the car safely and let the engine cool down. "
                          "Check the coolant level and look for leaks or a radiator fan that isn't spinning. "
                          "Continuing to drive can destroy the engine.",
    },
    {
        "key": "coolant_hot", "rule_type": RuleType.THRESHOLD,
        "name": "Coolant running hot",
        "description": "Coolant temperature above normal for a sustained period",
        "sensor_type": "coolant_temperature", "operator": ">",
        "threshold_value": 105, "duration_seconds": 300,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.HIGH,
        "auto_work_order": True,
        "recommendation": "Check the coolant level when the engine is cold, and make sure the "
                          "radiator isn't blocked. A sticking thermostat is a common cause.",
    },
    {
        "key": "battery_low", "rule_type": RuleType.THRESHOLD,
        "name": "Battery voltage low",
        "description": "Electrical system voltage below healthy level",
        "sensor_type": "battery_voltage", "operator": "<",
        "threshold_value": 11.8, "duration_seconds": 60,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.HIGH,
        "auto_work_order": True,
        "recommendation": "Have the battery and alternator tested. A battery below ~11.8 V "
                          "with the engine running often means the car won't start soon.",
    },
    {
        "key": "ecu_voltage_low", "rule_type": RuleType.THRESHOLD,
        "name": "Charging system weak",
        "description": "ECU supply voltage low (FMC001)",
        "sensor_type": "control_module_voltage", "operator": "<",
        "threshold_value": 12.0, "duration_seconds": 120,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.MEDIUM,
        "auto_work_order": True,
        "recommendation": "The engine computer is seeing low voltage. Check battery terminals "
                          "for corrosion and have the alternator output tested.",
    },
    {
        "key": "car_battery_low", "rule_type": RuleType.THRESHOLD,
        "name": "Car battery low",
        "description": "Vehicle-reported battery voltage low (FMC150 CAN)",
        "sensor_type": "vehicle_battery_voltage", "operator": "<",
        "threshold_value": 12.0, "duration_seconds": 120,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.MEDIUM,
        "auto_work_order": True,
        "recommendation": "The car itself reports low battery voltage. Have the battery "
                          "and charging system tested.",
    },
    {
        "key": "oil_temp_high", "rule_type": RuleType.THRESHOLD,
        "name": "Engine oil very hot",
        "description": "Oil temperature above safe sustained range",
        "sensor_type": "engine_oil_temperature", "operator": ">",
        "threshold_value": 130, "duration_seconds": 300,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.HIGH,
        "auto_work_order": True,
        "recommendation": "Ease off and let the engine cool. Check oil level; very hot oil "
                          "loses its ability to protect the engine.",
    },
    {
        "key": "service_due_soon", "rule_type": RuleType.THRESHOLD,
        "name": "Service due soon",
        "description": "Car-reported distance to next service below 500 km",
        "sensor_type": "distance_until_service", "operator": "<",
        "threshold_value": 500, "duration_seconds": 0,
        "severity": Severity.INFO, "priority": WorkOrderPriority.LOW,
        "auto_work_order": True,
        "recommendation": "Book your regular service — the car says it's due within 500 km.",
    },
    {
        "key": "service_interval", "rule_type": RuleType.SCHEDULED,
        "name": "Scheduled service interval",
        "description": "Regular maintenance every 10,000 km",
        "sensor_type": "odometer", "interval_value": 10000, "interval_unit": "km",
        "severity": Severity.INFO, "priority": WorkOrderPriority.MEDIUM,
        "auto_work_order": True,
        "recommendation": "Time for scheduled maintenance (engine oil, filters, inspection) "
                          "based on distance driven.",
    },
    {
        "key": "service_interval_hours", "rule_type": RuleType.SCHEDULED,
        "name": "Scheduled service by engine hours",
        "description": "Regular maintenance every 500 engine hours",
        "sensor_type": "engine_hours", "interval_value": 30000, "interval_unit": "engine_hours",
        "severity": Severity.INFO, "priority": WorkOrderPriority.MEDIUM,
        "auto_work_order": True,
        "recommendation": "Time for scheduled maintenance based on engine hours "
                          "(oil, filters, inspection).",
    },
    {
        "key": "dtc_any", "rule_type": RuleType.DTC,
        "name": "Fault code reported",
        "description": "Any diagnostic trouble code from the car",
        "dtc_code": None,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.MEDIUM,
        "auto_work_order": True,
        "recommendation": "The car's computer logged a fault. A mechanic (or an OBD app) can "
                          "read the exact cause — many are minor, but don't ignore it for weeks.",
    },
    {
        "key": "harsh_braking_day", "rule_type": RuleType.BEHAVIOR,
        "name": "Frequent hard braking today",
        "description": "More than 8 hard-braking events in one day",
        "sensor_type": "harsh_brake", "operator": ">=",
        "threshold_value": 8, "duration_seconds": 0,
        "severity": Severity.INFO, "priority": WorkOrderPriority.LOW,
        "auto_work_order": False,
        "recommendation": None,   # FYI only — no work order drafted
    },
    # Predictive maintenance (also ensured lazily by predictor.py)
    {
        "key": "predict_battery", "rule_type": RuleType.ANOMALY,
        "name": "Battery failure likely soon",
        "description": "Battery health heuristic from resting/crank voltage and short trips (advisory days, not measured RUL)",
        "sensor_type": "predict_battery", "operator": "<",
        "threshold_value": 40, "duration_seconds": 0,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.HIGH,
        "auto_work_order": True,
        "recommendation": "Test / replace battery — advisory health window is low.",
    },
    {
        "key": "predict_brakes", "rule_type": RuleType.ANOMALY,
        "name": "Brake pads wearing out",
        "description": "Friction-pad energy budget (hard + light braking, regen subtracted) since last service",
        "sensor_type": "predict_brakes", "operator": "<",
        "threshold_value": 25, "duration_seconds": 0,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.HIGH,
        "auto_work_order": True,
        "recommendation": "Inspect / replace brake pads — predictive wear model is low.",
    },
    {
        "key": "predict_oil", "rule_type": RuleType.ANOMALY,
        "name": "Oil change recommended",
        "description": "Oil service window from distance plus thermal/cold/idle/load stress (not oil chemistry)",
        "sensor_type": "predict_oil", "operator": "<",
        "threshold_value": 30, "duration_seconds": 0,
        "severity": Severity.WARNING, "priority": WorkOrderPriority.MEDIUM,
        "auto_work_order": True,
        "recommendation": "Oil & filter change — predictive oil health is low.",
    },
]


async def seed_presets(session: AsyncSession) -> None:
    """Insert missing preset rules; update editable fields left untouched."""
    existing = {
        r.key: r for r in (await session.execute(select(Rule))).scalars().all()
    }
    added = 0
    for preset in PRESETS:
        if preset["key"] in existing:
            continue
        session.add(Rule(**preset, is_active=True))
        added += 1
    if added:
        await session.commit()
        invalidate_rules_cache()
        logger.info("Seeded %d preset rules", added)