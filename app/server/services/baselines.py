"""
PREDICT — sensor baselines & anomaly detection (predictive maintenance).

Every few hours: compute 30-day per-car/per-sensor stats from the hourly
continuous aggregate, then run detectors that fire anomaly Alerts:
  z-score        any watched sensor sitting ≥3σ from its own baseline
  battery        voltage trending low vs baseline (fails weeks before a no-start)
  coolant creep  steady-state temperature elevated vs baseline (thermostat/radiator)
  fuel           recent trip L/100 km vs 7-day trip history
Reuses the normal Alert → suggested WorkOrder loop, so "abnormal for THIS
car" shows up exactly like any other detection.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import settings
from server.db import async_session_factory
from server.models import (
    Rule,
    RuleType,
    SensorBaseline,
    Severity,
    Trip,
    Vehicle,
    WorkOrderPriority,
)
from server.rules import fire_rule, invalidate_rules_cache

logger = logging.getLogger("predict.baselines")

BASELINE_SENSORS = (
    "battery_voltage", "control_module_voltage", "vehicle_battery_voltage",
    "coolant_temperature", "engine_oil_temperature",
    "fuel_level", "fuel_consumed", "fuel_rate",
)
WINDOW = "30d"
Z_SCORE_THRESHOLD = 3.0
SUSTAINED_HOURS = 3
ANOMALY_DEDUP_SECONDS = 24 * 3600

_task: Optional[asyncio.Task] = None
_fired_at: dict[tuple[int, str, str], datetime] = {}   # in-process dedupe


def _dedup_ok(vehicle_id: int, sensor_type: str, kind: str) -> bool:
    key = (vehicle_id, sensor_type, kind)
    last = _fired_at.get(key)
    if last and (datetime.now(timezone.utc) - last).total_seconds() < ANOMALY_DEDUP_SECONDS:
        return False
    _fired_at[key] = datetime.now(timezone.utc)
    return True


async def _ensure_rule(session: AsyncSession, key: str, name: str,
                       sensor_type: str, severity: Severity,
                       recommendation: Optional[str]) -> Rule:
    rule = (await session.execute(
        select(Rule).where(Rule.key == key)
    )).scalar_one_or_none()
    if rule:
        return rule
    rule = Rule(
        key=key, name=name, description=f"Automatic anomaly detector ({sensor_type})",
        rule_type=RuleType.ANOMALY, sensor_type=sensor_type,
        operator=">", threshold_value=Z_SCORE_THRESHOLD, duration_seconds=0,
        severity=severity, priority=WorkOrderPriority.MEDIUM,
        auto_work_order=bool(recommendation),
        recommendation=recommendation, is_active=True,
    )
    session.add(rule)
    await session.flush()
    invalidate_rules_cache()
    return rule


async def recompute_baselines(session: AsyncSession) -> int:
    vehicle_ids = (await session.execute(
        select(Vehicle.id)
    )).scalars().all()
    updated = 0
    for vid in vehicle_ids:
        for st in BASELINE_SENSORS:
            try:
                row = (await session.execute(text(
                    "SELECT avg(avg_value) AS mean, "
                    "       coalesce(stddev_samp(avg_value), 0) AS std, "
                    "       percentile_cont(0.95) WITHIN GROUP (ORDER BY avg_value) AS p95, "
                    "       count(*)::int AS n "
                    "FROM sensor_readings_1h "
                    "WHERE vehicle_id = :vid AND sensor_type = :st "
                    "  AND bucket >= now() - interval '30 days'"
                ), {"vid": vid, "st": st})).mappings().first()
            except Exception:
                row = (await session.execute(text(
                    "SELECT avg(value) AS mean, "
                    "       coalesce(stddev_samp(value), 0) AS std, "
                    "       percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95, "
                    "       count(*)::int AS n "
                    "FROM sensor_readings "
                    "WHERE vehicle_id = :vid AND sensor_type = :st "
                    "  AND timestamp >= now() - interval '30 days'"
                ), {"vid": vid, "st": st})).mappings().first()
            if not row or not row["n"] or row["mean"] is None:
                continue
            bl = (await session.execute(
                select(SensorBaseline).where(
                    SensorBaseline.vehicle_id == vid,
                    SensorBaseline.sensor_type == st,
                    SensorBaseline.window == WINDOW,
                )
            )).scalar_one_or_none()
            if bl is None:
                bl = SensorBaseline(vehicle_id=vid, sensor_type=st, window=WINDOW,
                                    mean=0.0, std=0.0)
                session.add(bl)
            bl.mean = float(row["mean"])
            bl.std = float(row["std"] or 0.0)
            bl.p95 = float(row["p95"]) if row["p95"] is not None else None
            bl.sample_count = int(row["n"])
            bl.updated_at = datetime.now(timezone.utc)
            updated += 1
    await session.flush()
    return updated


async def _recent_z(session: AsyncSession, vehicle_id: int, sensor_type: str,
                    baseline: SensorBaseline) -> Optional[float]:
    if baseline.std is None or baseline.std < 1e-9:
        return None
    try:
        row = (await session.execute(text(
            "SELECT avg(avg_value) FROM sensor_readings_1h "
            "WHERE vehicle_id = :vid AND sensor_type = :st "
            "  AND bucket >= now() - make_interval(hours => :h)"
        ), {"vid": vehicle_id, "st": sensor_type, "h": SUSTAINED_HOURS})).first()
    except Exception:
        row = (await session.execute(text(
            "SELECT avg(value) FROM sensor_readings "
            "WHERE vehicle_id = :vid AND sensor_type = :st "
            "  AND timestamp >= now() - make_interval(hours => :h)"
        ), {"vid": vehicle_id, "st": sensor_type, "h": SUSTAINED_HOURS})).first()
    if not row or row[0] is None:
        return None
    return (float(row[0]) - baseline.mean) / baseline.std


async def detect_anomalies(session: AsyncSession) -> int:
    fired = 0
    vehicles = list((await session.execute(select(Vehicle))).scalars().all())
    baselines = (await session.execute(select(SensorBaseline))).scalars().all()
    by_key = {(b.vehicle_id, b.sensor_type): b for b in baselines}
    now = datetime.now(timezone.utc)

    z_rule = await _ensure_rule(session, "anomaly_zscore", "Unusual sensor reading",
                                "generic", Severity.INFO, None)
    batt_rule = await _ensure_rule(
        session, "anomaly_battery", "Battery weakening over time",
        "battery_voltage", Severity.WARNING,
        "The battery's voltage trend is declining vs its own 30-day norm. Have the "
        "battery and charging system tested before it leaves you stranded.")
    cool_rule = await _ensure_rule(
        session, "anomaly_cooling", "Cooling system drifting",
        "coolant_temperature", Severity.WARNING,
        "Coolant temperature is creeping above this car's normal range — often an "
        "early sign of a sticking thermostat or a partially blocked radiator.")
    fuel_rule = await _ensure_rule(
        session, "anomaly_fuel", "Fuel consumption spike",
        "fuel_consumed", Severity.WARNING,
        "Recent fuel economy is far worse than this car's recent norm — could be "
        "a sensor, injector, or air-filter issue (or a fuel leak).")

    for v in vehicles:
        # Generic z-score
        for st in BASELINE_SENSORS:
            bl = by_key.get((v.id, st))
            if not bl:
                continue
            z = await _recent_z(session, v.id, st, bl)
            if z is not None and abs(z) >= Z_SCORE_THRESHOLD and _dedup_ok(v.id, st, "z"):
                await fire_rule(
                    session, vehicle_id=v.id, vehicle_name=v.name, rule=z_rule,
                    title=f"Unusual {st.replace('_', ' ')}",
                    message=f"{st.replace('_', ' ')} is {z:.1f}σ away from this car's "
                            f"30-day norm (norm ≈ {bl.mean:.2f}) — abnormal even though "
                            f"it may be inside global limits.",
                    trigger_value=round(z, 2))
                fired += 1

        # Battery degradation
        for st in ("battery_voltage", "control_module_voltage", "vehicle_battery_voltage"):
            bl = by_key.get((v.id, st))
            if not bl or bl.std < 0.05:
                continue
            z = await _recent_z(session, v.id, st, bl)
            if z is not None and z <= -1.5 and _dedup_ok(v.id, st, "batt"):
                await fire_rule(
                    session, vehicle_id=v.id, vehicle_name=v.name, rule=batt_rule,
                    title="Battery weakening over time",
                    message=f"{st.replace('_', ' ')} is trending below this car's "
                            f"30-day norm ({bl.mean:.2f} V baseline).",
                    trigger_value=round(z, 2))
                fired += 1

        # Cooling drift
        bl = by_key.get((v.id, "coolant_temperature"))
        if bl and bl.p95 is not None:
            z = await _recent_z(session, v.id, "coolant_temperature", bl)
            if z is not None and z >= 1.5 and _dedup_ok(v.id, "coolant_temperature", "creep"):
                await fire_rule(
                    session, vehicle_id=v.id, vehicle_name=v.name, rule=cool_rule,
                    title="Cooling system drifting",
                    message=f"Coolant steady-state is elevated vs this car's norm "
                            f"(p95 = {bl.p95:.1f} °C).",
                    trigger_value=round(z, 2))
                fired += 1

        # Fuel L/100 km from closed trips
        since = now - timedelta(days=7)
        trips = list((await session.execute(
            select(Trip).where(
                Trip.vehicle_id == v.id,
                Trip.is_open == False,  # noqa: E712
                Trip.end_ts >= since,
                Trip.distance_km.is_not(None), Trip.distance_km > 1,
                Trip.fuel_start.is_not(None), Trip.fuel_end.is_not(None),
            )
        )).scalars().all())
        rates = []
        for t in trips:
            delta = t.fuel_end - t.fuel_start   # fuel_consumed/liters: end ≥ start
            if delta and delta > 0:
                rates.append((delta / t.distance_km) * 100.0)
        if len(rates) >= 3:
            mean_r = sum(rates) / len(rates)
            var = sum((r - mean_r) ** 2 for r in rates) / (len(rates) - 1)
            std_r = var ** 0.5
            if std_r > 0.1 and abs(rates[-1] - mean_r) / std_r >= 3.0 and _dedup_ok(v.id, "fuel", "l100"):
                await fire_rule(
                    session, vehicle_id=v.id, vehicle_name=v.name, rule=fuel_rule,
                    title="Fuel consumption spike",
                    message=f"Latest trip used {rates[-1]:.1f} L/100 km vs a 7-day "
                            f"norm of {mean_r:.1f}.",
                    trigger_value=round(rates[-1], 2))
                fired += 1

    await session.commit()
    return fired


async def run_once() -> None:
    async with async_session_factory() as session:
        n = await recompute_baselines(session)
        await session.commit()
        fired = await detect_anomalies(session)
        logger.info("Baselines: %d updated, %d anomaly alert(s)", n, fired)


async def _loop() -> None:
    await asyncio.sleep(90)   # let startup settle
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Baselines job failed: %s", e)
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
