"""
PREDICT — cars: registration, detail, live vitals, history charts, timeline.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.deps import SessionDep
from server.catalog import GROUP_LABELS, sensor_meta
from server.config import settings
from server.ingest import registry
from server.models import (
    Alert,
    AlertStatus,
    ComponentHealth,
    DtcEvent,
    DriverScore,
    DrivingEvent,
    HealthEvent,
    MaintenanceLog,
    SensorReading,
    Trip,
    Vehicle,
    WorkOrder,
    WorkOrderStatus,
)
from server.schemas import CarCreate, CarUpdate
from server.state import live_store
from server.services import trips as trips_service
from server.services.predictor import compact_prognostics, prognostics_dict

logger = logging.getLogger("predict.api.cars")
router = APIRouter(prefix="/cars", tags=["cars"])


def _car_dict(v: Vehicle) -> dict:
    return {
        "id": v.id, "name": v.name, "license_plate": v.license_plate,
        "make": v.make, "model": v.model, "year": v.year, "vin": v.vin,
        "imei": v.imei, "device_type": v.device_type, "sim_phone": v.sim_phone,
        "mass_kg": v.mass_kg,
        "oil_capacity_l": v.oil_capacity_l,
        "brake_pad_capacity_mj": v.brake_pad_capacity_mj,
        "last_oil_change_at": v.last_oil_change_at.isoformat() if v.last_oil_change_at else None,
        "last_oil_change_odo": v.last_oil_change_odo,
        "last_brake_service_at": v.last_brake_service_at.isoformat() if v.last_brake_service_at else None,
        "last_brake_service_odo": v.last_brake_service_odo,
        "health": v.health.value,
        "last_seen": v.last_seen.isoformat() if v.last_seen else None,
        "created_at": v.created_at.isoformat() if v.created_at else None,
    }


async def _prognostics_by_vehicle(session: AsyncSession) -> dict[int, ComponentHealth]:
    rows = (await session.execute(select(ComponentHealth))).scalars().all()
    return {r.vehicle_id: r for r in rows}


async def _open_counts(session: AsyncSession) -> dict[int, dict]:
    """vehicle_id → {alerts, work_orders} in two grouped queries (no N+1)."""
    alert_rows = await session.execute(
        select(Alert.vehicle_id, func.count())
        .where(Alert.status == AlertStatus.ACTIVE)
        .group_by(Alert.vehicle_id)
    )
    wo_rows = await session.execute(
        select(WorkOrder.vehicle_id, func.count())
        .where(WorkOrder.status.in_([
            WorkOrderStatus.SUGGESTED, WorkOrderStatus.OPEN, WorkOrderStatus.IN_PROGRESS,
        ]))
        .group_by(WorkOrder.vehicle_id)
    )
    out: dict[int, dict] = {}
    for vid, c in alert_rows.all():
        out.setdefault(vid, {"alerts": 0, "work_orders": 0})["alerts"] = int(c)
    for vid, c in wo_rows.all():
        out.setdefault(vid, {"alerts": 0, "work_orders": 0})["work_orders"] = int(c)
    return out


# ── Registration & CRUD ───────────────────────────────────────────────────────
@router.post("")
async def register_car(body: CarCreate, session: AsyncSession = SessionDep):
    existing = (await session.execute(
        select(Vehicle).where(Vehicle.imei == body.imei)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "A car with this IMEI is already registered")

    vehicle = Vehicle(**body.model_dump())
    session.add(vehicle)
    await session.commit()
    registry.register(vehicle)
    logger.info("Car registered: %s (IMEI %s, %s)", vehicle.name, vehicle.imei, vehicle.device_type)
    return _car_dict(vehicle)


@router.get("")
async def list_cars(session: AsyncSession = SessionDep):
    vehicles = list((await session.execute(
        select(Vehicle).order_by(Vehicle.name)
    )).scalars().all())
    counts = await _open_counts(session)
    prog = await _prognostics_by_vehicle(session)
    return [
        {
            **_car_dict(v),
            "live": live_store.get(v.id).to_dict(settings.OFFLINE_AFTER_SECONDS),
            "open_alerts": counts.get(v.id, {}).get("alerts", 0),
            "open_work_orders": counts.get(v.id, {}).get("work_orders", 0),
            "prognostics": compact_prognostics(prog.get(v.id)),
        }
        for v in vehicles
    ]


@router.get("/{vehicle_id}")
async def get_car(vehicle_id: int, session: AsyncSession = SessionDep):
    v = await session.get(Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(404, "Car not found")
    counts = await _open_counts(session)
    today = datetime.now(timezone.utc).date()
    score = (await session.execute(
        select(DriverScore).where(
            DriverScore.vehicle_id == vehicle_id, DriverScore.date == today)
    )).scalar_one_or_none()
    health = await session.get(ComponentHealth, vehicle_id)
    return {
        **_car_dict(v),
        "live": live_store.get(v.id).to_dict(settings.OFFLINE_AFTER_SECONDS),
        "open_alerts": counts.get(v.id, {}).get("alerts", 0),
        "open_work_orders": counts.get(v.id, {}).get("work_orders", 0),
        "today_score": score.score if score else None,
        "prognostics": compact_prognostics(health),
    }


@router.get("/{vehicle_id}/prognostics")
async def car_prognostics(vehicle_id: int, session: AsyncSession = SessionDep):
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")
    health = await session.get(ComponentHealth, vehicle_id)
    payload = prognostics_dict(health)
    if payload is None:
        return {
            "vehicle_id": vehicle_id,
            "battery_score": None,
            "brake_score": None,
            "oil_score": None,
            "battery_rul_days": None,
            "brake_remaining_km": None,
            "oil_remaining_km": None,
            "drivers": {},
            "updated_at": None,
            "collecting": True,
        }
    return {"vehicle_id": vehicle_id, "collecting": False, **payload}


@router.patch("/{vehicle_id}")
async def update_car(vehicle_id: int, body: CarUpdate, session: AsyncSession = SessionDep):
    v = await session.get(Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(404, "Car not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(v, field, value)
    await session.commit()
    registry.register(v)
    return _car_dict(v)


@router.delete("/{vehicle_id}")
async def delete_car(vehicle_id: int, session: AsyncSession = SessionDep):
    v = await session.get(Vehicle, vehicle_id)
    if v is None:
        raise HTTPException(404, "Car not found")
    imei = v.imei
    await session.delete(v)   # FK cascades handle readings/issues/tasks/trips/…
    await session.commit()
    registry.remove(imei)
    live_store.remove(vehicle_id)
    trips_service.drop_state(vehicle_id)
    return {"deleted": vehicle_id}


# ── Live vitals (grouped for the car page) ────────────────────────────────────
@router.get("/{vehicle_id}/vitals")
async def car_vitals(vehicle_id: int, session: AsyncSession = SessionDep):
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")
    live = live_store.get(vehicle_id).to_dict(settings.OFFLINE_AFTER_SECONDS)
    groups: dict[str, list] = {}
    for st, sv in live["sensors"].items():
        meta = sensor_meta(st)
        groups.setdefault(meta["group"], []).append({
            "sensor_type": st,
            "name": meta["name"],
            "unit": sv["unit"] or meta["unit"],
            "decimals": meta["decimals"],
            "value": sv["value"],
            "ts": sv["ts"],
        })
    return {
        "live": live["live"],
        "last_seen": live["last_seen"],
        "ignition": live["ignition"],
        "gps": live["gps"],
        "groups": [
            {
                "group": g,
                "label": GROUP_LABELS.get(g, g.title()),
                "sensors": sorted(items, key=lambda s: s["name"]),
            }
            for g, items in sorted(groups.items())
        ],
    }


# ── History (auto resolution: raw ≤6h, 1m ≤7d, 1h ≤30d, 1d all-time) ─────────
@router.get("/{vehicle_id}/history")
async def car_history(
    vehicle_id: int,
    sensor_type: str = Query(...),
    hours: int = Query(default=24, ge=1, le=24 * 365 * 5),
    session: AsyncSession = SessionDep,
):
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)

    if hours <= 6:
        rows = (await session.execute(
            select(SensorReading.timestamp, SensorReading.value)
            .where(
                SensorReading.vehicle_id == vehicle_id,
                SensorReading.sensor_type == sensor_type,
                SensorReading.timestamp >= since,
            )
            .order_by(SensorReading.timestamp)
        )).all()
        points = [{"ts": ts.isoformat(), "value": v} for ts, v in rows]
        resolution = "raw"
    else:
        if hours <= 24 * 7:
            view = "sensor_readings_1m"
        elif hours <= 24 * 30:
            view = "sensor_readings_1h"
        else:
            view = "sensor_readings_1d"
        rows = (await session.execute(text(f"""
            SELECT bucket, avg_value, min_value, max_value
            FROM {view}
            WHERE vehicle_id = :vid AND sensor_type = :st AND bucket >= :since
            ORDER BY bucket
        """), {"vid": vehicle_id, "st": sensor_type, "since": since})).all()
        points = [
            {"ts": b.isoformat(), "value": round(float(a), 3),
             "min": round(float(lo), 3), "max": round(float(hi), 3)}
            for b, a, lo, hi in rows
        ]
        resolution = view.rsplit("_", 1)[-1]

    meta = sensor_meta(sensor_type)
    return {
        "sensor_type": sensor_type,
        "name": meta["name"],
        "unit": meta["unit"],
        "decimals": meta["decimals"],
        "resolution": resolution,
        "points": points,
    }


# ── Timeline (merged event stream for one car) ────────────────────────────────
@router.get("/{vehicle_id}/timeline")
async def car_timeline(
    vehicle_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = SessionDep,
):
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")

    events: list[dict] = []

    for i in (await session.execute(
        select(Alert).where(Alert.vehicle_id == vehicle_id)
        .order_by(Alert.created_at.desc()).limit(limit)
    )).scalars().all():
        events.append({
            "ts": i.created_at.isoformat(), "kind": "alert",
            "severity": i.severity.value, "title": i.title, "detail": i.message,
            "ref_id": i.id, "status": i.status.value,
        })

    for t in (await session.execute(
        select(WorkOrder).where(WorkOrder.vehicle_id == vehicle_id)
        .order_by(WorkOrder.created_at.desc()).limit(limit)
    )).scalars().all():
        events.append({
            "ts": t.created_at.isoformat(), "kind": "work_order",
            "severity": None, "title": t.title,
            "detail": f"Work order {t.status.value}", "ref_id": t.id,
            "status": t.status.value,
        })

    for m in (await session.execute(
        select(MaintenanceLog).where(MaintenanceLog.vehicle_id == vehicle_id)
        .order_by(MaintenanceLog.event_date.desc()).limit(limit)
    )).scalars().all():
        events.append({
            "ts": m.event_date.isoformat(), "kind": "maintenance",
            "severity": None, "title": m.title, "detail": m.notes,
            "ref_id": m.id, "status": "done",
        })

    for d in (await session.execute(
        select(DtcEvent).where(DtcEvent.vehicle_id == vehicle_id)
        .order_by(DtcEvent.timestamp.desc()).limit(limit)
    )).scalars().all():
        events.append({
            "ts": d.timestamp.isoformat(), "kind": "dtc",
            "severity": "warning", "title": f"Fault code {d.dtc_code}",
            "detail": None, "ref_id": d.id, "status": None,
        })

    for h in (await session.execute(
        select(HealthEvent).where(HealthEvent.vehicle_id == vehicle_id)
        .order_by(HealthEvent.timestamp.desc()).limit(limit)
    )).scalars().all():
        events.append({
            "ts": h.timestamp.isoformat(), "kind": "health",
            "severity": None,
            "title": f"Status: {h.from_health.value} → {h.to_health.value}",
            "detail": h.reason, "ref_id": h.id, "status": h.to_health.value,
        })

    for tr in (await session.execute(
        select(Trip).where(Trip.vehicle_id == vehicle_id, Trip.is_open == False)  # noqa: E712
        .order_by(Trip.start_ts.desc()).limit(50)
    )).scalars().all():
        dist = f"{tr.distance_km:.1f} km" if tr.distance_km is not None else "—"
        events.append({
            "ts": tr.start_ts.isoformat(), "kind": "trip",
            "severity": None, "title": f"Trip · {dist}",
            "detail": None, "ref_id": tr.id, "status": "closed",
        })

    events.sort(key=lambda e: e["ts"], reverse=True)
    return {"events": events[:limit]}


# ── Recent driving events for one car (used by timeline/driving drill-in) ────
@router.get("/{vehicle_id}/driving-events")
async def car_driving_events(
    vehicle_id: int,
    days: int = Query(default=7, ge=1, le=90),
    day: Optional[date] = Query(default=None, description="Local calendar day YYYY-MM-DD"),
    tz_offset: int = Query(default=0, ge=-840, le=840,
                           description="JS getTimezoneOffset() minutes"),
    limit: int = Query(default=100, ge=1, le=500),
    session: AsyncSession = SessionDep,
):
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")

    q = select(DrivingEvent).where(DrivingEvent.vehicle_id == vehicle_id)
    if day is not None:
        start = (datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
                 + timedelta(minutes=tz_offset))
        end = start + timedelta(days=1)
        q = q.where(DrivingEvent.ts >= start, DrivingEvent.ts < end)
        # Day drill-in: allow denser list than the default rolling window.
        limit = max(limit, 300)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        q = q.where(DrivingEvent.ts >= since)

    rows = (await session.execute(
        q.order_by(DrivingEvent.ts.desc()).limit(limit)
    )).scalars().all()
    return [
        {
            "id": e.id, "ts": e.ts.isoformat(), "event_type": e.event_type.value,
            "value": e.value, "latitude": e.latitude, "longitude": e.longitude,
            "source": e.source.value, "trip_id": e.trip_id,
        }
        for e in rows
    ]
