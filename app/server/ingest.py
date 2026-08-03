"""
PREDICT — ingest pipeline. One straight line, all in-process:

  Teltonika packet → decode (AVL map) → batch-insert readings → update live
  state → trips/behavior → device events → DTCs → rules → WS broadcast

Performance notes:
- Vehicles are cached in-process (IMEI → row); no per-packet lookup query.
- All readings of a packet land in ONE executemany INSERT.
- Rules are only evaluated for fresh records (store-and-forward bursts are
  stored and trip-processed, but never fire phantom issues).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from server.catalog import decode_record
from server.config import settings
from server.db import async_session_factory
from server.models import DtcEvent, SensorReading, Vehicle
from server.rules import evaluate_dtc, evaluate_telemetry
from server.services import trips
from server.services.health import recompute_and_broadcast
from server.state import live_store
from server.teltonika.codec8e import AvlRecord
from server.ws import hub

logger = logging.getLogger("predict.ingest")


# ── Vehicle registry (in-process IMEI cache) ─────────────────────────────────
class VehicleRegistry:
    def __init__(self) -> None:
        self._by_imei: dict[str, Vehicle] = {}
        self._unknown_logged: set[str] = set()

    async def refresh(self, session: AsyncSession) -> None:
        rows = (await session.execute(select(Vehicle))).scalars().all()
        self._by_imei = {v.imei: v for v in rows}
        self._unknown_logged.clear()
        logger.info("Vehicle registry: %d car(s)", len(self._by_imei))

    def register(self, vehicle: Vehicle) -> None:
        self._by_imei[vehicle.imei] = vehicle
        self._unknown_logged.discard(vehicle.imei)

    def remove(self, imei: str) -> None:
        self._by_imei.pop(imei, None)

    async def get(self, session: AsyncSession, imei: str) -> Optional[Vehicle]:
        v = self._by_imei.get(imei)
        if v is None:
            # Maybe registered since last refresh (or process restart)
            await self.refresh(session)
            v = self._by_imei.get(imei)
        return v

    def log_unknown_once(self, imei: str) -> None:
        if imei not in self._unknown_logged:
            self._unknown_logged.add(imei)
            logger.warning(
                "IMEI %s is sending data but isn't registered — add the car in "
                "Settings → Add car. Records are dropped.", imei,
            )


registry = VehicleRegistry()


# ── Packet handler (called by the Teltonika listener) ─────────────────────────
async def handle_records(imei: str, records: list[AvlRecord]) -> None:
    async with async_session_factory() as session:
        vehicle = await registry.get(session, imei)
        if vehicle is None:
            registry.log_unknown_once(imei)
            return

        model = (vehicle.device_type or "fmc001").lower()
        now = datetime.now(timezone.utc)
        max_age = settings.RULE_MAX_RECORD_AGE_SECONDS

        readings = [decode_record(r, model) for r in sorted(records, key=lambda r: r.timestamp)]

        # ── 1. Batch-insert all sensor rows (single executemany) ─────────────
        rows = [
            {
                "timestamp": rd.timestamp,
                "vehicle_id": vehicle.id,
                "sensor_type": st,
                "value": value,
                "unit": rd.units.get(st, ""),
                "latitude": rd.latitude,
                "longitude": rd.longitude,
                "speed": rd.speed,
                "ignition": rd.ignition,
            }
            for rd in readings
            for st, value in rd.sensors.items()
        ]
        if rows:
            await session.execute(insert(SensorReading), rows)

        # ── 2. Per-record: live state, trips, events, DTCs, rules ────────────
        latest = None
        for rd in readings:
            latest = rd
            live_store.update(
                vehicle.id,
                ts=rd.timestamp, sensors=rd.sensors, units=rd.units,
                ignition=rd.ignition, movement=rd.movement,
                latitude=rd.latitude, longitude=rd.longitude,
                speed=rd.speed, satellites=rd.satellites,
            )
            fresh = (now - rd.timestamp).total_seconds() <= max_age

            await trips.process_reading(
                session,
                vehicle_id=vehicle.id, ts=rd.timestamp, sensors=rd.sensors,
                ignition=rd.ignition, movement=rd.movement,
                latitude=rd.latitude, longitude=rd.longitude, gps_speed=rd.speed,
            )

            for ev in rd.events:
                await trips.record_device_event(
                    session,
                    vehicle_id=vehicle.id, ts=rd.timestamp,
                    event_type=ev["event_type"], value=ev.get("value"),
                    latitude=rd.latitude, longitude=rd.longitude,
                )

            for code in rd.dtcs:
                session.add(DtcEvent(
                    vehicle_id=vehicle.id, timestamp=rd.timestamp, dtc_code=code,
                ))
                if fresh:
                    await evaluate_dtc(
                        session,
                        vehicle_id=vehicle.id, vehicle_name=vehicle.name,
                        dtc_code=code, ts=rd.timestamp,
                    )

            if fresh:
                await evaluate_telemetry(
                    session,
                    vehicle_id=vehicle.id, vehicle_name=vehicle.name,
                    sensors=rd.sensors, units=rd.units, ts=rd.timestamp,
                )

        # ── 3. Vehicle bookkeeping + health ───────────────────────────────────
        # The registry entry is detached — re-fetch within THIS session so the
        # mutation actually persists.
        if latest is not None:
            db_vehicle = await session.get(Vehicle, vehicle.id)
            if db_vehicle is not None:
                if db_vehicle.last_seen is None or latest.timestamp > db_vehicle.last_seen:
                    db_vehicle.last_seen = latest.timestamp
                if latest.vin and db_vehicle.vin != latest.vin:
                    db_vehicle.vin = latest.vin
                await recompute_and_broadcast(session, db_vehicle.id, reason="telemetry")

        await session.commit()
        if latest is not None:
            registry.register(vehicle)   # keep cache fields fresh
            vehicle.last_seen = latest.timestamp
            if latest.vin:
                vehicle.vin = latest.vin

        # ── 4. Broadcast the merged live snapshot ─────────────────────────────
        if latest is not None:
            payload = live_store.get(vehicle.id).to_dict(settings.OFFLINE_AFTER_SECONDS)
            payload["vehicle_id"] = vehicle.id
            await hub.broadcast("telemetry", payload)

        logger.debug(
            "IMEI %s (%s): %d record(s), %d reading row(s)",
            imei, vehicle.name, len(records), len(rows),
        )
