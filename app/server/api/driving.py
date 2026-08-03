"""PREDICT — driving behavior: scores, trips, events."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.deps import SessionDep
from server.models import DriverScore, DrivingEvent, Trip, Vehicle

router = APIRouter(prefix="/driving", tags=["driving"])


def _local_day_range(d: date, tz_offset_minutes: int) -> tuple[datetime, datetime]:
    """Local calendar day → UTC [start, end).

    tz_offset_minutes matches JS Date#getTimezoneOffset (UTC − local, minutes).
    """
    start = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
             + timedelta(minutes=tz_offset_minutes))
    return start, start + timedelta(days=1)


@router.get("/summary")
async def driving_summary(session: AsyncSession = SessionDep):
    """Per-car scorecard for the Driving page."""
    vehicles = list((await session.execute(
        select(Vehicle).order_by(Vehicle.name)
    )).scalars().all())
    today = date.today()
    since = today - timedelta(days=13)

    score_rows = (await session.execute(
        select(DriverScore).where(DriverScore.date >= since)
    )).scalars().all()
    by_vehicle: dict[int, list] = {}
    for s in score_rows:
        by_vehicle.setdefault(s.vehicle_id, []).append(s)

    event_rows = await session.execute(
        select(DrivingEvent.vehicle_id, DrivingEvent.event_type, func.count())
        .where(DrivingEvent.ts >= datetime.combine(since, datetime.min.time(),
                                                   tzinfo=timezone.utc))
        .group_by(DrivingEvent.vehicle_id, DrivingEvent.event_type)
    )
    events_by_vehicle: dict[int, dict] = {}
    for vid, et, c in event_rows.all():
        events_by_vehicle.setdefault(vid, {})[et.value] = int(c)

    # Open trips aren't in DriverScore yet — still count them so the page
    # doesn't look empty while a drive is underway.
    open_rows = await session.execute(
        select(Trip.vehicle_id, func.count())
        .where(Trip.is_open == True)  # noqa: E712
        .group_by(Trip.vehicle_id)
    )
    open_by_vehicle = {vid: int(c) for vid, c in open_rows.all()}

    out = []
    for v in vehicles:
        scores = sorted(by_vehicle.get(v.id, []), key=lambda s: s.date)
        today_score = next((s for s in scores if s.date == today), None)
        recent = scores[-7:]
        closed_trips = sum(s.trips for s in scores)
        open_trips = open_by_vehicle.get(v.id, 0)
        out.append({
            "vehicle_id": v.id, "name": v.name,
            "today_score": today_score.score if today_score else None,
            "avg_score_7d": round(sum(s.score for s in recent) / len(recent), 1) if recent else None,
            "distance_14d_km": round(sum(s.distance_km for s in scores), 1),
            "trips_14d": closed_trips + open_trips,
            "open_trips": open_trips,
            "trend": [
                {"date": s.date.isoformat(), "score": s.score,
                 "distance_km": s.distance_km, "trips": s.trips}
                for s in scores
            ],
            "events_14d": events_by_vehicle.get(v.id, {}),
        })
    return out


@router.get("/cars/{vehicle_id}/calendar")
async def car_driving_calendar(
    vehicle_id: int,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    tz_offset: int = Query(default=0, ge=-840, le=840,
                           description="JS getTimezoneOffset() minutes"),
    session: AsyncSession = SessionDep,
):
    """Per-day trip/event activity for a month (local calendar days)."""
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")

    first = date(year, month, 1)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)

    range_start, _ = _local_day_range(first, tz_offset)
    _, range_end = _local_day_range(last, tz_offset)

    trips = (await session.execute(
        select(Trip).where(
            Trip.vehicle_id == vehicle_id,
            Trip.start_ts >= range_start,
            Trip.start_ts < range_end,
        )
    )).scalars().all()

    events = (await session.execute(
        select(DrivingEvent.ts).where(
            DrivingEvent.vehicle_id == vehicle_id,
            DrivingEvent.ts >= range_start,
            DrivingEvent.ts < range_end,
        )
    )).all()

    scores = (await session.execute(
        select(DriverScore).where(
            DriverScore.vehicle_id == vehicle_id,
            DriverScore.date >= first,
            DriverScore.date <= last,
        )
    )).scalars().all()
    score_by_day = {s.date.isoformat(): s for s in scores}

    # Bucket by local calendar day
    by_day: dict[str, dict] = {}
    for d in (first + timedelta(days=i) for i in range((last - first).days + 1)):
        key = d.isoformat()
        by_day[key] = {
            "date": key, "trips": 0, "events": 0,
            "distance_km": 0.0, "score": None,
        }
        sc = score_by_day.get(key)
        if sc:
            by_day[key]["score"] = sc.score
            by_day[key]["distance_km"] = round(sc.distance_km or 0.0, 1)

    for t in trips:
        local = t.start_ts - timedelta(minutes=tz_offset)
        key = local.date().isoformat()
        if key in by_day:
            by_day[key]["trips"] += 1
            if by_day[key]["score"] is None and t.distance_km:
                by_day[key]["distance_km"] = round(
                    by_day[key]["distance_km"] + (t.distance_km or 0), 1)

    for (ts,) in events:
        local = ts - timedelta(minutes=tz_offset)
        key = local.date().isoformat()
        if key in by_day:
            by_day[key]["events"] += 1

    return {
        "year": year,
        "month": month,
        "days": [by_day[d.isoformat()] for d in
                 (first + timedelta(days=i) for i in range((last - first).days + 1))],
    }


@router.get("/cars/{vehicle_id}/trips")
async def car_trips(
    vehicle_id: int,
    day: Optional[date] = Query(default=None, description="Local calendar day YYYY-MM-DD"),
    tz_offset: int = Query(default=0, ge=-840, le=840),
    limit: int = Query(default=50, ge=1, le=200),
    session: AsyncSession = SessionDep,
):
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")

    q = select(Trip).where(Trip.vehicle_id == vehicle_id)
    if day is not None:
        start, end = _local_day_range(day, tz_offset)
        q = q.where(Trip.start_ts >= start, Trip.start_ts < end)
    trips = (await session.execute(
        q.order_by(Trip.start_ts.desc()).limit(limit)
    )).scalars().all()

    trip_ids = [t.id for t in trips]
    event_counts: dict[int, int] = {}
    if trip_ids:
        rows = await session.execute(
            select(DrivingEvent.trip_id, func.count())
            .where(DrivingEvent.trip_id.in_(trip_ids))
            .group_by(DrivingEvent.trip_id)
        )
        event_counts = {tid: int(c) for tid, c in rows.all() if tid}

    return [
        {
            "id": t.id,
            "start_ts": t.start_ts.isoformat(),
            "end_ts": t.end_ts.isoformat() if t.end_ts else None,
            "is_open": t.is_open,
            "distance_km": t.distance_km,
            "duration_seconds": t.duration_seconds,
            "max_speed": t.max_speed,
            "avg_speed": t.avg_speed,
            "idle_seconds": t.idle_seconds,
            "fuel_used": (round(t.fuel_end - t.fuel_start, 2)
                          if t.fuel_start is not None and t.fuel_end is not None
                          and t.fuel_end >= t.fuel_start else None),
            "events": event_counts.get(t.id, 0),
        }
        for t in trips
    ]


@router.get("/cars/{vehicle_id}/scores")
async def car_scores(
    vehicle_id: int,
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = SessionDep,
):
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")
    since = date.today() - timedelta(days=days - 1)
    rows = (await session.execute(
        select(DriverScore)
        .where(DriverScore.vehicle_id == vehicle_id, DriverScore.date >= since)
        .order_by(DriverScore.date)
    )).scalars().all()
    return [
        {
            "date": s.date.isoformat(), "score": s.score, "trips": s.trips,
            "distance_km": s.distance_km, "idle_ratio": s.idle_ratio,
            "events_per_100km": s.events_per_100km,
        }
        for s in rows
    ]
