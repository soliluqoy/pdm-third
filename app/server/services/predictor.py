"""
PREDICT — Predictive Maintenance Engine (physics / heuristic models).

Three component health scorers (0–100, higher = healthier) + remaining-km /
advisory days:
  battery  resting voltage, crank drop, recovery, short-trip drain
           (advisory days are score buckets — not electrochemical RUL)
  brakes   cumulative friction KE since last pad service (regen subtracted)
  oil      distance schedule + thermal/cold/idle/load stress (not oil chemistry)

Triggers (never on the per-packet TCP hot path):
  • trip close  → brake energy for that trip + full vehicle refresh
  • hourly job  → refresh all vehicles (battery/oil catch-up)

Writes component_health + component_wear_events, then fires predict_* rules
via the normal Alert → suggested WorkOrder loop.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from server import settings_store
from server.config import settings
from server.db import async_session_factory
from server.models import (
    ComponentHealth,
    ComponentWearEvent,
    DrivingEvent,
    DrivingEventType,
    Rule,
    RuleType,
    Severity,
    Trip,
    Vehicle,
    WorkOrderPriority,
)
from server.rules import fire_rule, invalidate_rules_cache
from server.services.health import recompute_and_broadcast

logger = logging.getLogger("predict.predictor")

G = 9.80665
KMH_TO_MS = 1.0 / 3.6
VOLTAGE_SENSORS = (
    "vehicle_battery_voltage",
    "control_module_voltage",
    "battery_voltage",
)

_task: Optional[asyncio.Task] = None
_pending_trips: set[tuple[int, int]] = set()  # (vehicle_id, trip_id) in flight


# ── Config helpers ────────────────────────────────────────────────────────────
async def _cfg() -> dict[str, float]:
    return {
        "pad_mj": await settings_store.get_float("predict.brake_pad_capacity_mj", 800.0),
        "decel_g": await settings_store.get_float("predict.brake_decel_g", 0.25),
        "light_g": await settings_store.get_float("predict.light_brake_g", 0.10),
        "light_frac": await settings_store.get_float("predict.light_brake_fraction", 0.25),
        "regen_frac": await settings_store.get_float("predict.regen_fraction", 0.0),
        "batt_warn_days": await settings_store.get_float("predict.battery_warn_rul_days", 30.0),
        "oil_interval_km": await settings_store.get_float("predict.oil_interval_km", 10000.0),
        "mass_default": await settings_store.get_float("predict.mass_kg_default", 1500.0),
    }


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _advisory_days(score: float) -> int:
    """Non-physical advisory window from health score buckets (not true RUL)."""
    if score >= 80:
        return 90
    if score >= 50:
        return 30
    if score >= 30:
        return 14
    if score >= 15:
        return 7
    return 3


def _regen_fraction(vehicle: Vehicle, cfg: dict[str, float]) -> float:
    if vehicle.regen_fraction is not None:
        return max(0.0, min(0.95, float(vehicle.regen_fraction)))
    return max(0.0, min(0.95, float(cfg["regen_frac"])))


# ── Rule ensure ───────────────────────────────────────────────────────────────
async def _ensure_predict_rule(
    session: AsyncSession,
    key: str,
    name: str,
    recommendation: str,
    severity: Severity = Severity.WARNING,
    threshold: float = 40.0,
) -> Rule:
    rule = (await session.execute(select(Rule).where(Rule.key == key))).scalar_one_or_none()
    if rule:
        return rule
    rule = Rule(
        key=key,
        name=name,
        description=f"Predictive maintenance: {name}",
        rule_type=RuleType.ANOMALY,
        sensor_type=key,
        operator="<",
        threshold_value=threshold,
        duration_seconds=0,
        severity=severity,
        priority=WorkOrderPriority.HIGH,
        auto_work_order=True,
        recommendation=recommendation,
        is_active=True,
    )
    session.add(rule)
    await session.flush()
    invalidate_rules_cache()
    return rule


# ── Battery model ─────────────────────────────────────────────────────────────
async def _pick_voltage_sensor(session: AsyncSession, vehicle_id: int) -> Optional[str]:
    for st in VOLTAGE_SENSORS:
        n = (await session.execute(text(
            "SELECT count(*) FROM sensor_readings "
            "WHERE vehicle_id = :vid AND sensor_type = :st "
            "  AND timestamp >= now() - interval '14 days'"
        ), {"vid": vehicle_id, "st": st})).scalar() or 0
        if n > 0:
            return st
    return None


async def _resting_voltage(
    session: AsyncSession, vehicle_id: int, sensor: str,
) -> Optional[float]:
    """Median voltage while ignition is OFF (7d)."""
    row = (await session.execute(text("""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY value) AS med
        FROM sensor_readings
        WHERE vehicle_id = :vid AND sensor_type = :st
          AND timestamp >= now() - interval '7 days'
          AND (ignition IS FALSE OR ignition IS NULL)
          AND value BETWEEN 8 AND 16
    """), {"vid": vehicle_id, "st": sensor})).mappings().first()
    if not row or row["med"] is None:
        return None
    return float(row["med"])


async def _crank_drops(
    session: AsyncSession, vehicle_id: int, sensor: str, days: int = 14,
) -> list[float]:
    """Estimate crank voltage drops at ignition OFF→ON edges using trip starts."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    trips = list((await session.execute(
        select(Trip).where(
            Trip.vehicle_id == vehicle_id,
            Trip.start_ts >= since,
            Trip.is_open == False,  # noqa: E712
        ).order_by(Trip.start_ts)
    )).scalars().all())
    drops: list[float] = []
    for t in trips:
        # Bound windows in Python — asyncpg cannot do `:ts ± interval` safely.
        pre_from = t.start_ts - timedelta(minutes=2)
        crank_to = t.start_ts + timedelta(seconds=5)
        # Voltage just before start
        pre = (await session.execute(text("""
            SELECT value FROM sensor_readings
            WHERE vehicle_id = :vid AND sensor_type = :st
              AND timestamp >= :t_from AND timestamp <= :t0
              AND value BETWEEN 8 AND 16
            ORDER BY timestamp DESC LIMIT 1
        """), {
            "vid": vehicle_id, "st": sensor, "t_from": pre_from, "t0": t.start_ts,
        })).first()
        # Min voltage in first 5 s after start
        mn = (await session.execute(text("""
            SELECT min(value) FROM sensor_readings
            WHERE vehicle_id = :vid AND sensor_type = :st
              AND timestamp >= :t0 AND timestamp <= :t_to
              AND value BETWEEN 6 AND 16
        """), {
            "vid": vehicle_id, "st": sensor, "t0": t.start_ts, "t_to": crank_to,
        })).first()
        if pre and mn and pre[0] is not None and mn[0] is not None:
            drop = float(pre[0]) - float(mn[0])
            if 0 < drop < 6:
                drops.append(drop)
    return drops


async def _recovery_seconds(
    session: AsyncSession, vehicle_id: int, sensor: str, resting: Optional[float],
) -> Optional[float]:
    """After last closed trip, seconds until voltage nears resting."""
    if resting is None:
        return None
    trip = (await session.execute(
        select(Trip).where(
            Trip.vehicle_id == vehicle_id,
            Trip.is_open == False,  # noqa: E712
            Trip.end_ts.is_not(None),
        ).order_by(Trip.end_ts.desc()).limit(1)
    )).scalar_one_or_none()
    if not trip or not trip.end_ts:
        return None
    target = resting - 0.1
    window_end = trip.end_ts + timedelta(minutes=60)
    row = (await session.execute(text("""
        SELECT timestamp FROM sensor_readings
        WHERE vehicle_id = :vid AND sensor_type = :st
          AND timestamp > :t0 AND timestamp <= :t_to
          AND value >= :target
        ORDER BY timestamp ASC LIMIT 1
    """), {
        "vid": vehicle_id, "st": sensor,
        "t0": trip.end_ts, "t_to": window_end, "target": target,
    })).first()
    if not row:
        return 3600.0  # never recovered within an hour
    return max(0.0, (row[0] - trip.end_ts).total_seconds())


async def score_battery(
    session: AsyncSession, vehicle: Vehicle, cfg: dict[str, float],
) -> tuple[Optional[float], Optional[int], dict[str, Any]]:
    sensor = await _pick_voltage_sensor(session, vehicle.id)
    drivers: dict[str, Any] = {
        "sensor": sensor,
        "advisory_note": "Advisory window from health-score buckets; not electrochemical RUL",
    }

    if not sensor:
        # Unknown — do not invent a healthy score
        return None, None, {**drivers, "reason": "no_voltage_data", "top_reason": "No voltage data"}

    resting = await _resting_voltage(session, vehicle.id, sensor)
    drops = await _crank_drops(session, vehicle.id, sensor, days=14)
    avg_drop = sum(drops) / len(drops) if drops else None
    # Prior half-window for trend
    older = await _crank_drops(session, vehicle.id, sensor, days=28)
    older_only = older[: max(0, len(older) - len(drops))] if older else []
    older_avg = sum(older_only) / len(older_only) if older_only else None
    recovery = await _recovery_seconds(session, vehicle.id, sensor, resting)

    since = datetime.now(timezone.utc) - timedelta(days=7)
    trips = list((await session.execute(
        select(Trip).where(
            Trip.vehicle_id == vehicle.id,
            Trip.is_open == False,  # noqa: E712
            Trip.end_ts >= since,
        )
    )).scalars().all())
    short = sum(
        1 for t in trips
        if (t.duration_seconds or 0) < 600
    )
    short_ratio = (short / len(trips)) if trips else 0.0

    # Need at least one voltage-derived signal to score
    if resting is None and avg_drop is None and recovery is None:
        return None, None, {
            **drivers,
            "reason": "insufficient_voltage_signals",
            "top_reason": "Insufficient voltage signals",
            "short_trip_ratio_7d": round(short_ratio, 3),
            "trips_7d": len(trips),
        }

    score = 100.0
    # Resting voltage penalties
    if resting is not None:
        if resting < 12.0:
            score -= 40
        elif resting < 12.2:
            score -= 25
        elif resting < 12.4:
            score -= 10
    # Crank drop
    crank_pen = 0.0
    if avg_drop is not None:
        if avg_drop > 1.2:
            crank_pen = 25
        elif avg_drop > 0.8:
            crank_pen = 15
        if older_avg and older_avg > 0 and avg_drop >= older_avg * 1.2:
            crank_pen += 10
        score -= min(35, crank_pen)
    # Recovery
    if recovery is not None:
        if recovery > 1200:
            score -= 15
        elif recovery > 600:
            score -= 10
    # Short trips
    if short_ratio > 0.8:
        score -= 10
    elif short_ratio > 0.6:
        score -= 5

    score = _clamp(score)
    advisory = _advisory_days(score)
    drivers.update({
        "resting_v": round(resting, 3) if resting is not None else None,
        "crank_drop_v": round(avg_drop, 3) if avg_drop is not None else None,
        "crank_samples": len(drops),
        "recovery_s": round(recovery, 1) if recovery is not None else None,
        "short_trip_ratio_7d": round(short_ratio, 3),
        "trips_7d": len(trips),
    })
    if resting is not None and resting < 12.2:
        drivers["top_reason"] = "Low resting voltage"
    elif avg_drop is not None and avg_drop > 0.8:
        drivers["top_reason"] = "Deep crank voltage drop"
    elif short_ratio > 0.6:
        drivers["top_reason"] = "Frequent short trips"
    elif recovery is not None and recovery > 600:
        drivers["top_reason"] = "Slow charge recovery"
    else:
        drivers["top_reason"] = "Battery within normal range"

    return score, advisory, drivers


# ── Brake model ───────────────────────────────────────────────────────────────
async def accumulate_brake_energy_for_trip(
    session: AsyncSession,
    vehicle: Vehicle,
    trip: Trip,
    cfg: dict[str, float],
) -> float:
    """Sum friction-pad kinetic energy (MJ) during the trip.

    Counts full ΔKE for hard decelerations (≥ brake_decel_g) and a fraction
    for light braking. Hybrid regen_fraction is subtracted so only pad share
    accumulates toward the pad capacity budget.
    """
    if not trip.start_ts or not trip.end_ts:
        return 0.0
    mass = vehicle.mass_kg or cfg["mass_default"]
    hard_ms2 = cfg["decel_g"] * G
    light_ms2 = cfg["light_g"] * G
    light_frac = max(0.0, min(1.0, cfg["light_frac"]))
    regen = _regen_fraction(vehicle, cfg)
    pad_share = 1.0 - regen

    rows = (await session.execute(text("""
        SELECT timestamp, speed FROM sensor_readings
        WHERE vehicle_id = :vid
          AND timestamp >= :t0 AND timestamp <= :t1
          AND speed IS NOT NULL
        ORDER BY timestamp ASC
    """), {
        "vid": vehicle.id, "t0": trip.start_ts, "t1": trip.end_ts,
    })).all()

    energy_j = 0.0
    hard_events = 0
    light_events = 0
    prev_ts: Optional[datetime] = None
    prev_v: Optional[float] = None  # m/s

    for ts, speed_kmh in rows:
        if speed_kmh is None:
            continue
        v = float(speed_kmh) * KMH_TO_MS
        if prev_ts is not None and prev_v is not None:
            dt = (ts - prev_ts).total_seconds()
            if 0.5 <= dt <= 30 and prev_v > v:
                decel = (prev_v - v) / dt
                dke = 0.5 * mass * (prev_v * prev_v - v * v)
                if decel >= hard_ms2:
                    energy_j += dke
                    hard_events += 1
                elif decel >= light_ms2:
                    energy_j += dke * light_frac
                    light_events += 1
        prev_ts, prev_v = ts, v

    # Also count device harsh_brake events with a floor energy if no speed series
    harsh = (await session.execute(
        select(DrivingEvent).where(
            DrivingEvent.vehicle_id == vehicle.id,
            DrivingEvent.trip_id == trip.id,
            DrivingEvent.event_type == DrivingEventType.HARSH_BRAKE,
        )
    )).scalars().all()
    if hard_events == 0 and light_events == 0 and harsh:
        # Fallback: approximate each harsh event as a 50→20 km/h stop
        v_i, v_f = 50 * KMH_TO_MS, 20 * KMH_TO_MS
        energy_j += len(harsh) * 0.5 * mass * (v_i * v_i - v_f * v_f)
        hard_events = len(harsh)

    energy_mj = (energy_j * pad_share) / 1e6
    if energy_mj <= 0:
        return 0.0

    health = await _get_or_create_health(session, vehicle.id)
    health.brake_energy_mj_total = float(health.brake_energy_mj_total or 0.0) + energy_mj
    session.add(ComponentWearEvent(
        vehicle_id=vehicle.id,
        component="brakes",
        trip_id=trip.id,
        event_kind="wear",
        delta_score=None,
        metric={
            "energy_mj": round(energy_mj, 4),
            "hard_events": hard_events,
            "light_events": light_events,
            "events": hard_events + light_events,
            "regen_fraction": round(regen, 3),
            "distance_km": trip.distance_km,
        },
        ts=trip.end_ts or datetime.now(timezone.utc),
    ))
    await session.flush()
    return energy_mj


async def score_brakes(
    session: AsyncSession, vehicle: Vehicle, cfg: dict[str, float],
) -> tuple[float, Optional[int], dict[str, Any]]:
    health = await _get_or_create_health(session, vehicle.id)
    pad_mj = vehicle.brake_pad_capacity_mj or cfg["pad_mj"]
    total = float(health.brake_energy_mj_total or 0.0)
    score = _clamp(100.0 * (1.0 - total / max(pad_mj, 1e-6)))
    regen = _regen_fraction(vehicle, cfg)

    since = datetime.now(timezone.utc) - timedelta(days=7)
    trips = list((await session.execute(
        select(Trip).where(
            Trip.vehicle_id == vehicle.id,
            Trip.is_open == False,  # noqa: E712
            Trip.end_ts >= since,
        )
    )).scalars().all())
    dist_7d = sum(t.distance_km or 0.0 for t in trips)
    # Energy added in last 7d from wear ledger
    wear_rows = list((await session.execute(
        select(ComponentWearEvent).where(
            ComponentWearEvent.vehicle_id == vehicle.id,
            ComponentWearEvent.component == "brakes",
            ComponentWearEvent.event_kind == "wear",
            ComponentWearEvent.ts >= since,
        )
    )).scalars().all())
    energy_7d = sum(float((e.metric or {}).get("energy_mj", 0) or 0) for e in wear_rows)
    harsh_7d = (await session.execute(
        select(DrivingEvent).where(
            DrivingEvent.vehicle_id == vehicle.id,
            DrivingEvent.event_type == DrivingEventType.HARSH_BRAKE,
            DrivingEvent.ts >= since,
        )
    )).scalars().all()

    remaining_km: Optional[int] = None
    remaining_mj = max(0.0, pad_mj - total)
    if dist_7d > 1 and energy_7d > 0:
        mj_per_100 = energy_7d / dist_7d * 100.0
        if mj_per_100 > 1e-9:
            remaining_km = int(max(0, (remaining_mj / mj_per_100) * 100))

    drivers = {
        "energy_mj_total": round(total, 3),
        "energy_mj_7d": round(energy_7d, 3),
        "pad_capacity_mj": pad_mj,
        "regen_fraction": round(regen, 3),
        "harsh_brake_7d": len(harsh_7d),
        "distance_km_7d": round(dist_7d, 1),
        "top_reason": (
            "High braking energy this week" if energy_7d > 2
            else "Frequent hard braking" if len(harsh_7d) >= 8
            else "Brake wear within normal range"
        ),
    }
    return score, remaining_km, drivers


# ── Oil model (schedule + stress heuristics — not oil chemistry) ──────────────
async def score_oil(
    session: AsyncSession, vehicle: Vehicle, cfg: dict[str, float],
) -> tuple[float, Optional[int], dict[str, Any]]:
    """Distance-based service window plus thermal/cold/idle/load stress penalties.
    Not a viscosity/TAN/chemistry model; oil_capacity_l is unused."""
    interval = cfg["oil_interval_km"]
    sample_period = max(0.5, float(settings.TELEMETRY_SAMPLE_SECONDS))
    # Current odometer
    odo_row = (await session.execute(text("""
        SELECT value FROM sensor_readings
        WHERE vehicle_id = :vid AND sensor_type = 'odometer'
        ORDER BY timestamp DESC LIMIT 1
    """), {"vid": vehicle.id})).first()
    odo = float(odo_row[0]) if odo_row and odo_row[0] is not None else None

    if vehicle.last_oil_change_odo is not None and odo is not None:
        km_since = max(0.0, odo - float(vehicle.last_oil_change_odo))
    elif odo is not None and vehicle.created_at:
        # Fallback: distance since registration proxy (unknown prior change)
        km_since = 0.0
        # Use trip sum as proxy if no service anchor
        trips_all_dist = (await session.execute(text("""
            SELECT coalesce(sum(distance_km), 0) FROM trips
            WHERE vehicle_id = :vid AND is_open = false
        """), {"vid": vehicle.id})).scalar() or 0
        km_since = float(trips_all_dist)
    else:
        km_since = 0.0

    distance_pct = min(1.0, km_since / max(interval, 1.0))
    score = 100.0 - distance_pct * 60.0

    since = datetime.now(timezone.utc) - timedelta(days=7)

    # Thermal minutes: prefer 1m aggregates; raw fallback uses configured sample period
    thermal_min = 0.0
    for st in ("engine_oil_temperature", "coolant_temperature"):
        try:
            row = (await session.execute(text("""
                SELECT coalesce(sum(
                    CASE WHEN avg_value > 110 THEN samples ELSE 0 END
                ), 0)::float / 60.0 AS minutes
                FROM sensor_readings_1m
                WHERE vehicle_id = :vid AND sensor_type = :st
                  AND bucket >= now() - interval '7 days'
            """), {"vid": vehicle.id, "st": st})).first()
            if row and row[0]:
                thermal_min = max(thermal_min, float(row[0]))
        except Exception:
            row = (await session.execute(text("""
                SELECT count(*)::float * :period / 60.0 AS minutes
                FROM sensor_readings
                WHERE vehicle_id = :vid AND sensor_type = :st
                  AND timestamp >= now() - interval '7 days'
                  AND value > 110
            """), {"vid": vehicle.id, "st": st, "period": sample_period})).first()
            if row and row[0]:
                thermal_min = max(thermal_min, float(row[0]))

    thermal_pen = min(15.0, thermal_min * 0.5)
    score -= thermal_pen

    trips = list((await session.execute(
        select(Trip).where(
            Trip.vehicle_id == vehicle.id,
            Trip.is_open == False,  # noqa: E712
            Trip.end_ts >= since,
        )
    )).scalars().all())

    cold_short = 0
    idle_secs = 0
    dur_secs = 0
    for t in trips:
        dur = t.duration_seconds or 0
        idle_secs += t.idle_seconds or 0
        dur_secs += dur
        if dur > 0 and dur < 600:
            # Check max coolant during trip
            cool = (await session.execute(text("""
                SELECT max(value) FROM sensor_readings
                WHERE vehicle_id = :vid AND sensor_type = 'coolant_temperature'
                  AND timestamp >= :t0 AND timestamp <= :t1
            """), {
                "vid": vehicle.id, "t0": t.start_ts, "t1": t.end_ts or t.start_ts,
            })).first()
            max_cool = float(cool[0]) if cool and cool[0] is not None else None
            if max_cool is None or max_cool < 80:
                cold_short += 1

    cold_pen = min(15.0, cold_short * 2.0)
    score -= cold_pen

    idle_ratio = (idle_secs / dur_secs) if dur_secs > 0 else 0.0
    idle_pen = 5.0 if idle_ratio > 0.35 else (3.0 if idle_ratio > 0.2 else 0.0)
    score -= idle_pen

    # High load / high RPM time
    load_pen = 0.0
    try:
        rpm_row = (await session.execute(text("""
            SELECT coalesce(sum(
                CASE WHEN avg_value > 4000 THEN samples ELSE 0 END
            ), 0)::float / 60.0
            FROM sensor_readings_1m
            WHERE vehicle_id = :vid AND sensor_type = 'engine_rpm'
              AND bucket >= now() - interval '7 days'
        """), {"vid": vehicle.id})).first()
        rpm_min = float(rpm_row[0] or 0)
        load_row = (await session.execute(text("""
            SELECT coalesce(sum(
                CASE WHEN avg_value > 80 THEN samples ELSE 0 END
            ), 0)::float / 60.0
            FROM sensor_readings_1m
            WHERE vehicle_id = :vid AND sensor_type = 'engine_load'
              AND bucket >= now() - interval '7 days'
        """), {"vid": vehicle.id})).first()
        load_min = float(load_row[0] or 0)
        load_pen = min(5.0, (rpm_min + load_min) * 0.3)
    except Exception:
        load_pen = 0.0
    score -= load_pen

    score = _clamp(score)

    # Remaining km until score ~20 at current pace
    remaining_km: Optional[int] = None
    if km_since > 50 and score < 100:
        wear_per_km = (100.0 - score) / max(km_since, 1.0)
        if wear_per_km > 1e-9:
            remaining_km = int(max(0, (score - 20.0) / wear_per_km))
    elif score >= 90:
        remaining_km = int(max(0, interval - km_since))

    drivers = {
        "km_since_change": round(km_since, 1),
        "interval_km": interval,
        "cold_short_trips_7d": cold_short,
        "thermal_minutes_7d": round(thermal_min, 1),
        "idle_ratio_7d": round(idle_ratio, 3),
        "load_penalty": round(load_pen, 2),
        "model": "schedule_plus_stress",
        "top_reason": (
            "Oil change due by distance" if distance_pct > 0.7
            else "Frequent short cold trips" if cold_short >= 5
            else "High thermal stress" if thermal_pen >= 5
            else "Oil within service window"
        ),
    }
    return score, remaining_km, drivers


# ── Persist + fire ────────────────────────────────────────────────────────────
async def _get_or_create_health(session: AsyncSession, vehicle_id: int) -> ComponentHealth:
    row = await session.get(ComponentHealth, vehicle_id)
    if row is None:
        row = ComponentHealth(
            vehicle_id=vehicle_id,
            battery_score=None,
            brake_score=None,
            oil_score=None,
            battery_rul_days=None,
            brake_energy_mj_total=0.0,
            drivers={},
        )
        session.add(row)
        await session.flush()
    return row


async def _maybe_fire(
    session: AsyncSession,
    vehicle: Vehicle,
    *,
    key: str,
    name: str,
    recommendation: str,
    score: float,
    threshold: float,
    severity: Severity,
    extra_msg: str,
    force: bool = False,
) -> None:
    if not force and score >= threshold:
        return
    rule = await _ensure_predict_rule(
        session, key, name, recommendation, severity=severity, threshold=threshold,
    )
    await fire_rule(
        session,
        vehicle_id=vehicle.id,
        vehicle_name=vehicle.name,
        rule=rule,
        title=name,
        message=extra_msg,
        trigger_value=round(score, 1),
        severity=severity,
    )


async def update_vehicle(
    session: AsyncSession,
    vehicle_id: int,
    *,
    trip_id: Optional[int] = None,
    accumulate_trip_brakes: bool = False,
) -> Optional[ComponentHealth]:
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle is None:
        return None
    cfg = await _cfg()

    if accumulate_trip_brakes and trip_id is not None:
        trip = await session.get(Trip, trip_id)
        if trip and trip.vehicle_id == vehicle_id:
            await accumulate_brake_energy_for_trip(session, vehicle, trip, cfg)

    batt_score, batt_advisory, batt_drv = await score_battery(session, vehicle, cfg)
    brake_score, brake_km, brake_drv = await score_brakes(session, vehicle, cfg)
    oil_score, oil_km, oil_drv = await score_oil(session, vehicle, cfg)

    health = await _get_or_create_health(session, vehicle_id)
    prev_batt = health.battery_score
    prev_brake = health.brake_score
    prev_oil = health.oil_score

    health.battery_score = round(batt_score, 1) if batt_score is not None else None
    health.brake_score = round(brake_score, 1)
    health.oil_score = round(oil_score, 1)
    health.battery_rul_days = batt_advisory  # advisory window, not physical RUL
    health.brake_remaining_km = brake_km
    health.oil_remaining_km = oil_km
    health.drivers = {
        "battery": batt_drv,
        "brakes": brake_drv,
        "oil": oil_drv,
    }
    health.updated_at = datetime.now(timezone.utc)

    # Ledger score snapshots (only when material change)
    now = health.updated_at
    for comp, prev, cur, metric in (
        ("battery", prev_batt, batt_score, batt_drv),
        ("brakes", prev_brake, brake_score, brake_drv),
        ("oil", prev_oil, oil_score, oil_drv),
    ):
        if cur is None:
            continue
        if prev is None or abs(float(prev) - cur) >= 0.5:
            session.add(ComponentWearEvent(
                vehicle_id=vehicle_id,
                component=comp,
                trip_id=trip_id,
                event_kind="score",
                delta_score=round(cur - float(prev), 2) if prev is not None else None,
                metric=metric,
                ts=now,
            ))

    await session.flush()

    # Fire predictive rules (fire_rule commits — call after flush)
    warn_days = int(cfg["batt_warn_days"])
    if batt_score is not None and (
        batt_score < 40
        or (batt_advisory is not None and batt_advisory < warn_days)
    ):
        await _maybe_fire(
            session, vehicle,
            key="predict_battery",
            name="Battery failure likely soon",
            recommendation=(
                f"Test / replace battery — advisory window ~{batt_advisory} days "
                f"(heuristic, not measured RUL). {batt_drv.get('top_reason', '')}."
            ),
            score=batt_score,
            threshold=40.0,
            severity=Severity.CRITICAL if batt_score < 25 else Severity.WARNING,
            extra_msg=(
                f"Battery health {batt_score:.0f}/100 · advisory ~{batt_advisory} days. "
                f"{batt_drv.get('top_reason', '')}."
            ),
            force=batt_advisory is not None and batt_advisory < warn_days,
        )
    if brake_score < 25:
        km_txt = f"~{brake_km} km left" if brake_km is not None else "inspect soon"
        await _maybe_fire(
            session, vehicle,
            key="predict_brakes",
            name="Brake pads wearing out",
            recommendation=f"Inspect / replace brake pads — {km_txt}.",
            score=brake_score,
            threshold=25.0,
            severity=Severity.WARNING,
            extra_msg=(
                f"Brake health {brake_score:.0f}/100 · {km_txt}. "
                f"{brake_drv.get('top_reason', '')}."
            ),
        )
    if oil_score < 30:
        km_txt = f"~{oil_km} km remaining" if oil_km is not None else "due soon"
        await _maybe_fire(
            session, vehicle,
            key="predict_oil",
            name="Oil change recommended",
            recommendation=f"Oil & filter change — {km_txt}.",
            score=oil_score,
            threshold=30.0,
            severity=Severity.WARNING,
            extra_msg=(
                f"Oil health {oil_score:.0f}/100 · {km_txt}. "
                f"{oil_drv.get('top_reason', '')}."
            ),
        )

    # Fold prognostics into fleet RAG (skip unknown battery)
    await recompute_and_broadcast(
        session, vehicle_id, reason="prognostics",
        prognostics={
            k: v for k, v in {
                "battery": batt_score, "brakes": brake_score, "oil": oil_score,
            }.items() if v is not None
        },
    )
    return health


async def reset_component(
    session: AsyncSession,
    vehicle_id: int,
    component: str,
    *,
    odometer: Optional[float] = None,
) -> None:
    """Called when a predictive work order is completed."""
    vehicle = await session.get(Vehicle, vehicle_id)
    if vehicle is None:
        return
    health = await _get_or_create_health(session, vehicle_id)
    now = datetime.now(timezone.utc)

    if component == "brakes":
        health.brake_energy_mj_total = 0.0
        health.brake_score = 100.0
        health.brake_remaining_km = None
        vehicle.last_brake_service_at = now
        if odometer is not None:
            vehicle.last_brake_service_odo = odometer
    elif component == "oil":
        health.oil_score = 100.0
        health.oil_remaining_km = None
        vehicle.last_oil_change_at = now
        if odometer is not None:
            vehicle.last_oil_change_odo = odometer
    elif component == "battery":
        health.battery_score = 100.0
        health.battery_rul_days = 90  # advisory after service reset
    else:
        return

    session.add(ComponentWearEvent(
        vehicle_id=vehicle_id,
        component=component,
        trip_id=None,
        event_kind="reset",
        delta_score=None,
        metric={"odometer": odometer},
        ts=now,
    ))
    health.updated_at = now
    await session.flush()
    logger.info("Reset %s prognostics for vehicle %d", component, vehicle_id)


def component_for_rule_key(rule_key: Optional[str]) -> Optional[str]:
    if not rule_key:
        return None
    return {
        "predict_battery": "battery",
        "predict_brakes": "brakes",
        "predict_oil": "oil",
    }.get(rule_key)


def prognostics_dict(h: Optional[ComponentHealth]) -> Optional[dict]:
    if h is None:
        return None
    return {
        "battery_score": h.battery_score,
        "brake_score": h.brake_score,
        "oil_score": h.oil_score,
        "battery_rul_days": h.battery_rul_days,
        "brake_remaining_km": h.brake_remaining_km,
        "oil_remaining_km": h.oil_remaining_km,
        "brake_energy_mj_total": h.brake_energy_mj_total,
        "drivers": h.drivers or {},
        "updated_at": h.updated_at.isoformat() if h.updated_at else None,
    }


def compact_prognostics(h: Optional[ComponentHealth]) -> Optional[dict]:
    if h is None:
        return None
    return {
        "battery": h.battery_score,
        "brakes": h.brake_score,
        "oil": h.oil_score,
        "battery_rul_days": h.battery_rul_days,
    }


# ── Job entry points ──────────────────────────────────────────────────────────
async def on_trip_closed(vehicle_id: int, trip_id: int) -> None:
    key = (vehicle_id, trip_id)
    if key in _pending_trips:
        return
    _pending_trips.add(key)
    try:
        async with async_session_factory() as session:
            await update_vehicle(
                session, vehicle_id,
                trip_id=trip_id, accumulate_trip_brakes=True,
            )
            await session.commit()
            logger.info("PME updated vehicle %d after trip %d", vehicle_id, trip_id)
    except Exception as e:
        logger.error("PME trip-close failed for vehicle %d: %s", vehicle_id, e)
    finally:
        _pending_trips.discard(key)


async def run_once() -> None:
    async with async_session_factory() as session:
        ids = list((await session.execute(select(Vehicle.id))).scalars().all())
    for vid in ids:
        try:
            async with async_session_factory() as session:
                await update_vehicle(session, vid)
                await session.commit()
        except Exception as e:
            logger.error("PME batch failed for vehicle %d: %s", vid, e)
    logger.info("PME batch complete (%d vehicle(s))", len(ids))


async def _loop() -> None:
    await asyncio.sleep(120)  # let startup + baselines settle
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("PME job failed: %s", e)
        await asyncio.sleep(settings.PREDICTOR_INTERVAL_SECONDS)


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
