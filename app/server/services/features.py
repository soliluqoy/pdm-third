"""
PREDICT — feature engineering for the failure-prediction models.

Computes a per-vehicle, per-day feature vector (VehicleFeature) from:
  - telemetry aggregates (battery, coolant, oil temp, RPM)
  - trips (distance, duration, count)
  - driving events (harsh events, idle ratio)
  - baselines (z-score drift from the 30-day norm)

This is the ML anomaly model input. One row per (vehicle, day). Failure labels
(failed / failure_component) are written when a reactive work order completes
(and re-synced by models.evaluate).

Runs as a nightly job (see start()/run_once()) and can be invoked on demand.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import settings
from server.db import async_session_factory
from server.models import (
    DrivingEvent,
    SensorBaseline,
    Trip,
    Vehicle,
    VehicleFeature,
)

logger = logging.getLogger("predict.features")

# Sensor types we aggregate per day (normalized names from the AVL maps).
BATTERY_SENSORS = ("battery_voltage", "control_module_voltage", "vehicle_battery_voltage")
COOLANT_SENSOR = "coolant_temperature"
OIL_TEMP_SENSOR = "engine_oil_temperature"
RPM_SENSOR = "engine_rpm"
ODOMETER_SENSOR = "odometer"
ENGINE_HOURS_SENSOR = "engine_hours"

# How many days of history to backfill on first run.
BACKFILL_DAYS = 30

_task: Optional[asyncio.Task] = None


# ── Per-day telemetry aggregates ──────────────────────────────────────────────
async def _day_telemetry(session: AsyncSession, vehicle_id: int, day: date) -> dict:
    """Aggregate sensor readings for one vehicle on one UTC day."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    out: dict = {}

    # Battery: mean / min / std over the day (any of the voltage sensors).
    batt = (await session.execute(text("""
        SELECT avg(value) AS mean, min(value) AS min, stddev_samp(value) AS std
        FROM sensor_readings
        WHERE vehicle_id = :vid
          AND sensor_type IN ('battery_voltage','control_module_voltage','vehicle_battery_voltage')
          AND timestamp >= :t0 AND timestamp < :t1
          AND value BETWEEN 8 AND 16
    """), {"vid": vehicle_id, "t0": start, "t1": end})).mappings().first()
    if batt and batt["mean"] is not None:
        out["battery_mean_v"] = float(batt["mean"])
        out["battery_min_v"] = float(batt["min"])
        out["battery_std_v"] = float(batt["std"] or 0.0)

    # Coolant: mean / p95 / max.
    cool = (await session.execute(text("""
        SELECT avg(value) AS mean,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95,
               max(value) AS max
        FROM sensor_readings
        WHERE vehicle_id = :vid AND sensor_type = 'coolant_temperature'
          AND timestamp >= :t0 AND timestamp < :t1
          AND value BETWEEN -40 AND 150
    """), {"vid": vehicle_id, "t0": start, "t1": end})).mappings().first()
    if cool and cool["mean"] is not None:
        out["coolant_mean_c"] = float(cool["mean"])
        out["coolant_p95_c"] = float(cool["p95"] or cool["mean"])
        out["coolant_max_c"] = float(cool["max"] or cool["mean"])

    # Oil temp: mean / max.
    oil = (await session.execute(text("""
        SELECT avg(value) AS mean, max(value) AS max
        FROM sensor_readings
        WHERE vehicle_id = :vid AND sensor_type = 'engine_oil_temperature'
          AND timestamp >= :t0 AND timestamp < :t1
          AND value BETWEEN -40 AND 200
    """), {"vid": vehicle_id, "t0": start, "t1": end})).mappings().first()
    if oil and oil["mean"] is not None:
        out["oil_temp_mean_c"] = float(oil["mean"])
        out["oil_temp_max_c"] = float(oil["max"] or oil["mean"])

    # RPM: mean / max.
    rpm = (await session.execute(text("""
        SELECT avg(value) AS mean, max(value) AS max
        FROM sensor_readings
        WHERE vehicle_id = :vid AND sensor_type = 'engine_rpm'
          AND timestamp >= :t0 AND timestamp < :t1
          AND value BETWEEN 0 AND 12000
    """), {"vid": vehicle_id, "t0": start, "t1": end})).mappings().first()
    if rpm and rpm["mean"] is not None:
        out["rpm_mean"] = float(rpm["mean"])
        out["rpm_max"] = float(rpm["max"] or rpm["mean"])

    # Thermal / high-RPM minutes: count × configured sample period / 60.
    sample_period = max(0.5, float(settings.TELEMETRY_SAMPLE_SECONDS))
    therm = (await session.execute(text("""
        SELECT count(*)::float * :period / 60.0 AS minutes
        FROM sensor_readings
        WHERE vehicle_id = :vid
          AND sensor_type IN ('engine_oil_temperature','coolant_temperature')
          AND timestamp >= :t0 AND timestamp < :t1
          AND value > 110
    """), {"vid": vehicle_id, "t0": start, "t1": end, "period": sample_period})).first()
    if therm and therm[0]:
        out["thermal_minutes"] = float(therm[0])

    # High-RPM minutes (above 4000).
    hrpm = (await session.execute(text("""
        SELECT count(*)::float * :period / 60.0 AS minutes
        FROM sensor_readings
        WHERE vehicle_id = :vid AND sensor_type = 'engine_rpm'
          AND timestamp >= :t0 AND timestamp < :t1
          AND value > 4000
    """), {"vid": vehicle_id, "t0": start, "t1": end, "period": sample_period})).first()
    if hrpm and hrpm[0]:
        out["high_rpm_minutes"] = float(hrpm[0])

    # Latest odometer / engine hours of the day.
    for sensor, key in ((ODOMETER_SENSOR, "odometer"), (ENGINE_HOURS_SENSOR, "engine_hours")):
        row = (await session.execute(text("""
            SELECT value FROM sensor_readings
            WHERE vehicle_id = :vid AND sensor_type = :st
              AND timestamp >= :t0 AND timestamp < :t1
            ORDER BY timestamp DESC LIMIT 1
        """), {"vid": vehicle_id, "st": sensor, "t0": start, "t1": end})).first()
        if row and row[0] is not None:
            out[key] = float(row[0])

    return out


# ── Per-day trip + behavior aggregates ────────────────────────────────────────
async def _day_trips(session: AsyncSession, vehicle_id: int, day: date) -> dict:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    out: dict = {}

    trips = list((await session.execute(
        select(Trip).where(
            Trip.vehicle_id == vehicle_id,
            Trip.start_ts >= start,
            Trip.start_ts < end,
            Trip.is_open == False,  # noqa: E712
        )
    )).scalars().all())
    out["trip_count"] = len(trips)
    out["distance_km"] = round(sum(t.distance_km or 0.0 for t in trips), 3)
    out["duration_seconds"] = sum(t.duration_seconds or 0 for t in trips)

    # Harsh events (accel/brake/corner) that day.
    harsh = (await session.execute(
        select(DrivingEvent).where(
            DrivingEvent.vehicle_id == vehicle_id,
            DrivingEvent.ts >= start,
            DrivingEvent.ts < end,
            DrivingEvent.event_type.in_(["harsh_accel", "harsh_brake", "harsh_corner"]),
        )
    )).scalars().all()
    out["harsh_events"] = len(harsh)

    # Idle ratio.
    dur = out["duration_seconds"]
    idle = sum(t.idle_seconds or 0 for t in trips)
    out["idle_ratio"] = round(idle / dur, 4) if dur > 0 else 0.0

    return out


# ── Trend (rate of change) over the trailing window ──────────────────────────
async def _trend(session: AsyncSession, vehicle_id: int, sensor: str,
                 days: int = 7) -> Optional[float]:
    """Slope of the daily mean of a sensor over the last N days (units/day).
    Uses the 1-day continuous aggregate when available, else raw readings."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    try:
        rows = (await session.execute(text("""
            SELECT bucket, avg_value FROM sensor_readings_1d
            WHERE vehicle_id = :vid AND sensor_type = :st AND bucket >= :since
            ORDER BY bucket
        """), {"vid": vehicle_id, "st": sensor, "since": since})).all()
    except Exception:
        rows = (await session.execute(text("""
            SELECT date_trunc('day', timestamp) AS bucket, avg(value) AS avg_value
            FROM sensor_readings
            WHERE vehicle_id = :vid AND sensor_type = :st AND timestamp >= :since
            GROUP BY 1 ORDER BY 1
        """), {"vid": vehicle_id, "st": sensor, "since": since})).all()
    if len(rows) < 2:
        return None
    # Simple linear regression slope.
    n = len(rows)
    xs = list(range(n))
    ys = [float(r[1]) for r in rows if r[1] is not None]
    if len(ys) < 2:
        return None
    x_mean = sum(xs) / n
    y_mean = sum(ys) / n
    num = sum((xs[i] - x_mean) * (ys[i] - y_mean) for i in range(len(ys)))
    den = sum((xs[i] - x_mean) ** 2 for i in range(len(ys)))
    if den == 0:
        return None
    return num / den


# ── Z-score drift from the 30-day baseline ────────────────────────────────────
async def _baseline_z(session: AsyncSession, vehicle_id: int, sensor: str,
                      day: date) -> Optional[float]:
    bl = (await session.execute(
        select(SensorBaseline).where(
            SensorBaseline.vehicle_id == vehicle_id,
            SensorBaseline.sensor_type == sensor,
            SensorBaseline.window == "30d",
        )
    )).scalar_one_or_none()
    if bl is None or bl.std is None or bl.std < 1e-9:
        return None
    # Recent mean (last 3 days) vs baseline.
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc) - timedelta(days=3)
    row = (await session.execute(text("""
        SELECT avg(value) FROM sensor_readings
        WHERE vehicle_id = :vid AND sensor_type = :st AND timestamp >= :since
    """), {"vid": vehicle_id, "st": sensor, "since": start})).first()
    if not row or row[0] is None:
        return None
    return (float(row[0]) - bl.mean) / bl.std


# ── Build one day's feature row ───────────────────────────────────────────────
async def build_day(session: AsyncSession, vehicle_id: int, day: date) -> Optional[VehicleFeature]:
    tele = await _day_telemetry(session, vehicle_id, day)
    trips = await _day_trips(session, vehicle_id, day)

    # Skip days with no data at all (no telemetry and no trips).
    if not tele and trips.get("trip_count", 0) == 0:
        return None

    row = VehicleFeature(
        vehicle_id=vehicle_id,
        date=day,
        odometer=tele.get("odometer"),
        engine_hours=tele.get("engine_hours"),
        distance_km=trips.get("distance_km"),
        trip_count=trips.get("trip_count", 0),
        duration_seconds=trips.get("duration_seconds", 0),
        battery_mean_v=tele.get("battery_mean_v"),
        battery_min_v=tele.get("battery_min_v"),
        battery_std_v=tele.get("battery_std_v"),
        battery_trend_v_per_day=await _trend(session, vehicle_id, "battery_voltage"),
        coolant_mean_c=tele.get("coolant_mean_c"),
        coolant_p95_c=tele.get("coolant_p95_c"),
        coolant_max_c=tele.get("coolant_max_c"),
        coolant_trend_c_per_day=await _trend(session, vehicle_id, "coolant_temperature"),
        oil_temp_mean_c=tele.get("oil_temp_mean_c"),
        oil_temp_max_c=tele.get("oil_temp_max_c"),
        thermal_minutes=tele.get("thermal_minutes"),
        rpm_mean=tele.get("rpm_mean"),
        rpm_max=tele.get("rpm_max"),
        high_rpm_minutes=tele.get("high_rpm_minutes"),
        harsh_events=trips.get("harsh_events", 0),
        idle_ratio=trips.get("idle_ratio", 0.0),
        battery_z=await _baseline_z(session, vehicle_id, "battery_voltage", day),
        coolant_z=await _baseline_z(session, vehicle_id, "coolant_temperature", day),
        oil_temp_z=await _baseline_z(session, vehicle_id, "engine_oil_temperature", day),
    )
    return row


# ── Job entry points ──────────────────────────────────────────────────────────
async def run_once(backfill_days: int = BACKFILL_DAYS) -> int:
    """Compute feature rows for the last N days for every vehicle (upsert)."""
    async with async_session_factory() as session:
        vehicle_ids = list((await session.execute(select(Vehicle.id))).scalars().all())
        today = date.today()
        written = 0
        for vid in vehicle_ids:
            for offset in range(backfill_days):
                day = today - timedelta(days=offset)
                existing = (await session.execute(
                    select(VehicleFeature).where(
                        VehicleFeature.vehicle_id == vid,
                        VehicleFeature.date == day,
                    )
                )).scalar_one_or_none()
                if existing is not None:
                    continue
                row = await build_day(session, vid, day)
                if row is not None:
                    session.add(row)
                    written += 1
            await session.commit()
        logger.info("Features: %d new row(s) for %d vehicle(s)", written, len(vehicle_ids))
        return written


async def _loop() -> None:
    await asyncio.sleep(150)   # let startup + baselines settle
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Features job failed: %s", e)
        await asyncio.sleep(settings.BASELINES_INTERVAL_SECONDS)


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