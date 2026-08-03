"""
PREDICT — in-process live state (replaces Redis from the old stack).

One merged snapshot per vehicle. Key property: records legitimately omit IO
elements, so updates MERGE per sensor (each sensor keeps its own timestamp)
instead of replacing the whole snapshot — tiles never blank out on partial
records. Staleness is judged per snapshot age, and per sensor if needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class SensorValue:
    value: float
    unit: str
    ts: datetime


@dataclass
class VehicleLive:
    sensors: dict[str, SensorValue] = field(default_factory=dict)
    ignition: Optional[bool] = None
    movement: Optional[bool] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed: Optional[float] = None
    satellites: Optional[int] = None
    last_seen: Optional[datetime] = None
    connected_imei: bool = False   # tracker TCP session currently open

    def to_dict(self, max_age_seconds: int) -> dict:
        now = datetime.now(timezone.utc)
        age = (now - self.last_seen).total_seconds() if self.last_seen else None
        live = age is not None and age <= max_age_seconds
        return {
            "live": live,
            "age_seconds": round(age) if age is not None else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
            "ignition": self.ignition if live else None,
            "movement": self.movement if live else None,
            "connected": self.connected_imei,
            "gps": {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "speed": self.speed,
                "satellites": self.satellites,
            } if live else None,
            "sensors": {
                st: {
                    "value": sv.value,
                    "unit": sv.unit,
                    "ts": sv.ts.isoformat(),
                }
                for st, sv in self.sensors.items()
            } if live else {},
        }


class LiveStore:
    def __init__(self) -> None:
        self._vehicles: dict[int, VehicleLive] = {}

    def get(self, vehicle_id: int) -> VehicleLive:
        vl = self._vehicles.get(vehicle_id)
        if vl is None:
            vl = VehicleLive()
            self._vehicles[vehicle_id] = vl
        return vl

    def update(
        self,
        vehicle_id: int,
        *,
        ts: datetime,
        sensors: dict[str, float],
        units: dict[str, str],
        ignition: Optional[bool],
        movement: Optional[bool],
        latitude: Optional[float],
        longitude: Optional[float],
        speed: Optional[float],
        satellites: Optional[int],
    ) -> VehicleLive:
        vl = self.get(vehicle_id)
        for st, value in sensors.items():
            vl.sensors[st] = SensorValue(value=value, unit=units.get(st, ""), ts=ts)
        if ignition is not None:
            vl.ignition = ignition
        if movement is not None:
            vl.movement = movement
        if latitude and longitude:
            vl.latitude, vl.longitude = latitude, longitude
        if speed is not None:
            vl.speed = speed
        if satellites is not None:
            vl.satellites = satellites
        if vl.last_seen is None or ts > vl.last_seen:
            vl.last_seen = ts
        return vl

    def set_connected(self, vehicle_id: int, connected: bool) -> None:
        self.get(vehicle_id).connected_imei = connected

    def remove(self, vehicle_id: int) -> None:
        self._vehicles.pop(vehicle_id, None)


live_store = LiveStore()
