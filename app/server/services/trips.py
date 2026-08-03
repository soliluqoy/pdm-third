"""
PREDICT — trips & driving behavior.

Trip segmentation (ignition transitions, with a movement/speed fallback),
derived driving events (harsh accel/brake, speeding, idling, high RPM),
device-native eco-driving events, and the daily 0–100 driving score.

Per-vehicle rolling state lives in-process (the old stack used Redis): one
event loop, one writer per vehicle, so a dict is both safe and faster.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server import settings_store
from server.models import (
    DriverScore,
    DrivingEvent,
    DrivingEventSource,
    DrivingEventType,
    Trip,
    Vehicle,
)
from server.rules import evaluate_behavior
from server.ws import hub

logger = logging.getLogger("predict.trips")

TRIP_GAP_SECONDS = 300      # movement/speed fallback gap
SPEED_NEAR_ZERO = 3.0       # km/h
SPEEDING_CONSECUTIVE = 2    # samples above limit before it counts

_states: dict[int, dict] = {}


def _state(vehicle_id: int) -> dict:
    return _states.setdefault(vehicle_id, {})


def drop_state(vehicle_id: int) -> None:
    _states.pop(vehicle_id, None)


# ── Config (from settings store) ──────────────────────────────────────────────
async def _cfg() -> dict:
    return {
        "speed_limit_kmh": await settings_store.get_float("behavior.speed_limit_kmh", 120),
        "idle_minutes": await settings_store.get_float("behavior.idle_minutes", 5),
        "accel_threshold_ms2": await settings_store.get_float("behavior.accel_threshold_ms2", 3.0),
        "high_rpm_threshold": await settings_store.get_float("behavior.high_rpm_threshold", 4000),
        "score_weights": await settings_store.get_json("behavior.score_weights", {}),
    }


def _speed(sensors: dict[str, float], gps_speed: Optional[float]) -> Optional[float]:
    for key in ("vehicle_speed_obd", "vehicle_speed"):
        v = sensors.get(key)
        if v is not None:
            return v
    return gps_speed


def _fuel(sensors: dict[str, float]) -> Optional[float]:
    for key in ("fuel_consumed", "fuel_level_liters", "fuel_level"):
        v = sensors.get(key)
        if v is not None:
            return v
    return None


async def _open_trip(session: AsyncSession, vehicle_id: int) -> Optional[Trip]:
    result = await session.execute(
        select(Trip)
        .where(Trip.vehicle_id == vehicle_id, Trip.is_open == True)  # noqa: E712
        .order_by(Trip.start_ts.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _add_event(session, *, vehicle_id, trip_id, ts, event_type, value,
                     latitude, longitude, source) -> DrivingEvent:
    ev = DrivingEvent(
        vehicle_id=vehicle_id, trip_id=trip_id, ts=ts,
        event_type=event_type, value=value,
        latitude=latitude, longitude=longitude, source=source,
    )
    session.add(ev)
    await session.flush()
    await hub.broadcast("driving_event", {
        "vehicle_id": vehicle_id, "trip_id": trip_id,
        "event_type": event_type.value, "value": value,
        "ts": ts.isoformat(), "source": source.value,
    })
    return ev


# ── Device-native eco-driving events (Tier 1, accurate) ───────────────────────
async def record_device_event(
    session: AsyncSession,
    *,
    vehicle_id: int,
    ts: datetime,
    event_type: str,
    value: Optional[float],
    latitude: Optional[float],
    longitude: Optional[float],
) -> None:
    try:
        et = DrivingEventType(event_type)
    except ValueError:
        logger.warning("Unknown device event_type=%r", event_type)
        return
    trip = await _open_trip(session, vehicle_id)
    await _add_event(
        session, vehicle_id=vehicle_id, trip_id=trip.id if trip else None,
        ts=ts, event_type=et, value=value,
        latitude=latitude, longitude=longitude,
        source=DrivingEventSource.DEVICE,
    )
    if trip:
        await recompute_daily_score(session, vehicle_id, ts.date())


# ── Per-record processing (Tier 2 derivation) ────────────────────────────────
async def process_reading(
    session: AsyncSession,
    *,
    vehicle_id: int,
    ts: datetime,
    sensors: dict[str, float],
    ignition: Optional[bool],
    movement: Optional[bool],
    latitude: Optional[float],
    longitude: Optional[float],
    gps_speed: Optional[float],
) -> None:
    cfg = await _cfg()
    state = _state(vehicle_id)

    speed = _speed(sensors, gps_speed)
    odometer = sensors.get("odometer")
    fuel = _fuel(sensors)
    rpm = sensors.get("engine_rpm")

    prev_ign = state.get("last_ignition")
    prev_ts: Optional[datetime] = state.get("last_ts")
    prev_speed = state.get("last_speed")

    trip = await _open_trip(session, vehicle_id)

    # ── Trip boundaries ───────────────────────────────────────────────────────
    start_trip = False
    end_trip = False
    if ignition is True and prev_ign is not True:
        # First sighting of ignition-on (or an on→off→on edge): trip begins.
        start_trip = True
    elif ignition is False and prev_ign is True:
        end_trip = True
    else:
        gap = (ts - prev_ts).total_seconds() if prev_ts else None
        moving = movement is True or (speed is not None and speed > SPEED_NEAR_ZERO)
        stopped = (movement is False or movement is None) and (
            speed is None or speed <= SPEED_NEAR_ZERO
        )
        if trip is None and gap is not None and gap > TRIP_GAP_SECONDS and moving:
            start_trip = True
        if trip is not None and gap is not None and gap > TRIP_GAP_SECONDS and stopped:
            end_trip = True

    if start_trip and trip is None:
        trip = Trip(
            vehicle_id=vehicle_id, start_ts=ts,
            start_odometer=odometer, fuel_start=fuel,
            idle_seconds=0, is_open=True, max_speed=0.0, avg_speed=0.0,
        )
        session.add(trip)
        await session.flush()
        state.update(trip_id=trip.id, speed_sum=0.0, speed_n=0, idle_accum=0.0,
                     idle_since=None, speeding_streak=0, high_rpm_latched=False,
                     last_idle_event_key=None)
        await hub.broadcast("trip", {
            "vehicle_id": vehicle_id, "trip_id": trip.id, "action": "started",
            "ts": ts.isoformat(),
        })

    if end_trip and trip is not None:
        await _close_trip(session, trip, ts, odometer, fuel, state)
        trip = None

    # ── In-trip aggregates + derived events ───────────────────────────────────
    if trip is not None:
        if speed is not None:
            state["speed_sum"] = state.get("speed_sum", 0.0) + speed
            state["speed_n"] = state.get("speed_n", 0) + 1
            if speed > state.get("max_speed", 0.0):
                state["max_speed"] = speed
                trip.max_speed = speed

        # Harsh accel / brake: Δspeed/Δt between samples
        if (speed is not None and prev_speed is not None and prev_ts is not None
                and (ts - prev_ts).total_seconds() > 0):
            dt = (ts - prev_ts).total_seconds()
            accel = ((speed - prev_speed) * (1000.0 / 3600.0)) / dt  # km/h→m/s per s
            thr = cfg["accel_threshold_ms2"]
            if accel > thr:
                await _add_event(session, vehicle_id=vehicle_id, trip_id=trip.id, ts=ts,
                                 event_type=DrivingEventType.HARSH_ACCEL, value=round(accel, 3),
                                 latitude=latitude, longitude=longitude,
                                 source=DrivingEventSource.DERIVED)
            elif accel < -thr:
                await _add_event(session, vehicle_id=vehicle_id, trip_id=trip.id, ts=ts,
                                 event_type=DrivingEventType.HARSH_BRAKE, value=round(abs(accel), 3),
                                 latitude=latitude, longitude=longitude,
                                 source=DrivingEventSource.DERIVED)

        # Speeding streak
        if speed is not None and speed > cfg["speed_limit_kmh"]:
            state["speeding_streak"] = state.get("speeding_streak", 0) + 1
            if state["speeding_streak"] == SPEEDING_CONSECUTIVE:
                await _add_event(session, vehicle_id=vehicle_id, trip_id=trip.id, ts=ts,
                                 event_type=DrivingEventType.SPEEDING, value=speed,
                                 latitude=latitude, longitude=longitude,
                                 source=DrivingEventSource.DERIVED)
        else:
            state["speeding_streak"] = 0

        # High RPM while moving
        moving = speed is not None and speed > SPEED_NEAR_ZERO
        if moving and rpm is not None and rpm >= cfg["high_rpm_threshold"]:
            if not state.get("high_rpm_latched"):
                await _add_event(session, vehicle_id=vehicle_id, trip_id=trip.id, ts=ts,
                                 event_type=DrivingEventType.HIGH_RPM, value=rpm,
                                 latitude=latitude, longitude=longitude,
                                 source=DrivingEventSource.DERIVED)
                state["high_rpm_latched"] = True
        else:
            state["high_rpm_latched"] = False

        # Idling: engine on (or in-trip) + near-zero speed
        ign_on = ignition is True or (ignition is None and trip is not None)
        if ign_on and (speed is None or speed <= SPEED_NEAR_ZERO):
            if state.get("idle_since") is None:
                state["idle_since"] = ts
            idle_secs = (ts - state["idle_since"]).total_seconds()
            idle_key = state["idle_since"].isoformat()
            if idle_secs >= cfg["idle_minutes"] * 60 and state.get("last_idle_event_key") != idle_key:
                await _add_event(session, vehicle_id=vehicle_id, trip_id=trip.id, ts=ts,
                                 event_type=DrivingEventType.IDLING,
                                 value=round(idle_secs / 60, 2),
                                 latitude=latitude, longitude=longitude,
                                 source=DrivingEventSource.DERIVED)
                state["last_idle_event_key"] = idle_key
            if prev_ts is not None:
                dt = (ts - prev_ts).total_seconds()
                if 0 < dt < 120:
                    state["idle_accum"] = state.get("idle_accum", 0.0) + dt
                    trip.idle_seconds = int(state["idle_accum"])
        else:
            state["idle_since"] = None

    # Persist rolling state
    if ignition is not None:
        state["last_ignition"] = ignition
    if movement is not None:
        state["last_movement"] = movement
    if speed is not None:
        state["last_speed"] = speed
    state["last_ts"] = ts


async def _close_trip(session, trip: Trip, ts, odometer, fuel, state) -> None:
    trip.end_ts = ts
    trip.end_odometer = odometer
    trip.fuel_end = fuel
    trip.is_open = False
    if trip.start_odometer is not None and odometer is not None and odometer >= trip.start_odometer:
        trip.distance_km = round(odometer - trip.start_odometer, 3)
    trip.duration_seconds = max(0, int((ts - trip.start_ts).total_seconds()))
    trip.idle_seconds = int(state.get("idle_accum", 0))
    n = state.get("speed_n", 0)
    if n > 0:
        trip.avg_speed = round(state.get("speed_sum", 0.0) / n, 2)
    trip.max_speed = state.get("max_speed")
    await session.flush()
    state["trip_id"] = None
    await hub.broadcast("trip", {
        "vehicle_id": trip.vehicle_id, "trip_id": trip.id, "action": "ended",
        "ts": ts.isoformat(), "distance_km": trip.distance_km,
        "duration_seconds": trip.duration_seconds,
    })
    await recompute_daily_score(session, trip.vehicle_id, trip.start_ts.date())

    # Daily behavior rules (e.g. harsh_braking_day) — was defined but never called
    vehicle = await session.get(Vehicle, trip.vehicle_id)
    if vehicle is not None:
        await evaluate_behavior(
            session,
            vehicle_id=vehicle.id,
            vehicle_name=vehicle.name,
            ts=ts,
        )

    # Predictive maintenance: accumulate brake energy + refresh scores (own session)
    from server.services import predictor
    asyncio.create_task(predictor.on_trip_closed(trip.vehicle_id, trip.id))


# ── Daily score ───────────────────────────────────────────────────────────────
async def recompute_daily_score(session: AsyncSession, vehicle_id: int, day: date) -> DriverScore:
    cfg = await _cfg()
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    trips = list((await session.execute(
        select(Trip).where(
            Trip.vehicle_id == vehicle_id,
            Trip.start_ts >= start, Trip.start_ts < end,
            Trip.is_open == False,  # noqa: E712
        )
    )).scalars().all())
    distance = sum(t.distance_km or 0.0 for t in trips)
    idle_secs = sum(t.idle_seconds or 0 for t in trips)
    duration = sum(t.duration_seconds or 0 for t in trips)

    rows = await session.execute(
        select(DrivingEvent.event_type, func.count())
        .where(DrivingEvent.vehicle_id == vehicle_id,
               DrivingEvent.ts >= start, DrivingEvent.ts < end)
        .group_by(DrivingEvent.event_type)
    )
    counts = {et.value if hasattr(et, "value") else str(et): int(c) for et, c in rows.all()}

    denom = max(distance, 1.0)
    weights = cfg["score_weights"] or {}
    per_100: dict[str, float] = {}
    penalty = 0.0
    for etype, count in counts.items():
        rate = (count / denom) * 100.0
        per_100[etype] = round(rate, 2)
        penalty += rate * float(weights.get(etype, 0))
    idle_ratio = (idle_secs / duration) if duration > 0 else 0.0
    penalty += idle_ratio * 20.0
    score = max(0.0, min(100.0, 100.0 - penalty))

    row = (await session.execute(
        select(DriverScore).where(
            DriverScore.vehicle_id == vehicle_id, DriverScore.date == day)
    )).scalar_one_or_none()
    if row is None:
        row = DriverScore(vehicle_id=vehicle_id, date=day)
        session.add(row)
    row.trips = len(trips)
    row.distance_km = round(distance, 3)
    row.events_per_100km = per_100
    row.idle_ratio = round(idle_ratio, 4)
    row.score = round(score, 1)
    await session.flush()
    return row
