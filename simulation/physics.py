"""
Simple longitudinal vehicle physics for a continuous urban/highway trip.

Models speed, position (great-circle), engine RPM/load, thermal lag for
coolant/oil, fuel burn, and battery voltage under crank / charge / rest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# Earth radius (km) for lat/lon integration
_R_KM = 6371.0


@dataclass
class VehicleState:
    """Physical + sensor state at one instant."""
    ts: datetime
    lat: float
    lon: float
    heading_deg: float
    speed_kmh: float
    accel_ms2: float
    odometer_km: float
    ignition: bool
    movement: bool

    # Engine / thermal
    rpm: float
    engine_load: float
    throttle: float
    coolant_c: float
    oil_temp_c: float
    intake_air_c: float
    ambient_c: float
    runtime_s: float

    # Electrical
    battery_v: float
    ecu_v: float
    tracker_battery_v: float

    # Fuel / service
    fuel_level_pct: float
    fuel_rate_lph: float
    distance_until_service_km: float

    # GNSS
    altitude_m: int = 25
    satellites: int = 14

    # Optional one-shot event overlays (AVL green-driving / DTC)
    green_driving_type: Optional[int] = None  # 1 accel, 2 brake, 3 corner
    green_driving_value: Optional[int] = None
    overspeed_kmh: Optional[int] = None
    dtc_codes: list[str] = field(default_factory=list)
    vin: Optional[str] = None


@dataclass
class PhysicsConfig:
    mass_kg: float = 1400.0
    drag_cd_a: float = 0.75          # Cd * A rough
    rolling_crr: float = 0.015
    max_accel_ms2: float = 3.2
    max_brake_ms2: float = 7.5
    idle_rpm: float = 800.0
    redline_rpm: float = 6200.0
    coolant_ambient_gain: float = 0.04   # 1/s toward target
    oil_ambient_gain: float = 0.025
    fuel_tank_l: float = 50.0
    idle_fuel_lph: float = 0.8
    load_fuel_lph: float = 12.0


class VehiclePhysics:
    """Integrates a target speed profile into realistic sensor values."""

    def __init__(
        self,
        *,
        lat0: float = 14.5547,
        lon0: float = 121.0244,
        heading0: float = 90.0,
        odometer_km: float = 49850.0,
        distance_until_service_km: float = 450.0,
        fuel_pct: float = 68.0,
        ambient_c: float = 32.0,
        vin: str = "JTDBR32E720012345",
        cfg: Optional[PhysicsConfig] = None,
    ) -> None:
        self.cfg = cfg or PhysicsConfig()
        self.lat = lat0
        self.lon = lon0
        self.heading = heading0
        self.speed_ms = 0.0
        self.odometer_km = odometer_km
        self.distance_until_service_km = distance_until_service_km
        self.fuel_pct = fuel_pct
        self.ambient_c = ambient_c
        self.coolant_c = ambient_c
        self.oil_temp_c = ambient_c
        self.intake_air_c = ambient_c
        self.runtime_s = 0.0
        self.ignition = False
        self.battery_v = 12.55
        self.ecu_v = 12.50
        self.tracker_battery_v = 4.05
        self.vin = vin
        self._crank_remaining_s = 0.0
        self._resting = True

        # Overlays set by scenario for alert phases
        self.force_coolant_c: Optional[float] = None
        self.force_oil_temp_c: Optional[float] = None
        self.force_battery_v: Optional[float] = None
        self.force_ecu_v: Optional[float] = None
        self.force_distance_until_service: Optional[float] = None
        self.dtc_codes: list[str] = []
        self.green_event: Optional[tuple[int, int]] = None
        self.overspeed_event: Optional[int] = None
        self.include_vin = True

    def set_ignition(self, on: bool, *, crank: bool = False) -> None:
        if on and not self.ignition:
            self.runtime_s = 0.0
            if crank:
                # Starter draws the battery down for ~1.5 s of sim time
                self._crank_remaining_s = 1.5
                self.battery_v = min(self.battery_v, 12.4) - 1.35
                self.ecu_v = self.battery_v + 0.05
        if not on and self.ignition:
            self._resting = True
        self.ignition = on
        if not on:
            self.speed_ms = 0.0

    def step(self, dt: float, target_speed_kmh: float, ts: datetime) -> VehicleState:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        cfg = self.cfg
        target_ms = max(0.0, target_speed_kmh) / 3.6

        # Longitudinal control toward target
        if not self.ignition:
            target_ms = 0.0

        dv = target_ms - self.speed_ms
        if dv >= 0:
            accel = min(cfg.max_accel_ms2, dv / max(dt, 1e-3))
        else:
            accel = max(-cfg.max_brake_ms2, dv / max(dt, 1e-3))

        # Drag + rolling when moving
        if self.speed_ms > 0.1:
            drag = (0.5 * 1.2 * cfg.drag_cd_a * self.speed_ms * self.speed_ms) / cfg.mass_kg
            roll = cfg.rolling_crr * 9.81
            if accel > 0:
                accel = max(0.0, accel - drag - roll)

        self.speed_ms = max(0.0, self.speed_ms + accel * dt)
        if abs(self.speed_ms - target_ms) < 0.05:
            self.speed_ms = target_ms

        # Position integration
        dist_m = self.speed_ms * dt
        if dist_m > 0:
            self._advance(dist_m)
            self.odometer_km += dist_m / 1000.0
            if self.force_distance_until_service is None:
                self.distance_until_service_km = max(
                    0.0, self.distance_until_service_km - dist_m / 1000.0
                )

        # Engine
        speed_kmh = self.speed_ms * 3.6
        if not self.ignition:
            rpm = 0.0
            load = 0.0
            throttle = 0.0
            fuel_rate = 0.0
        elif speed_kmh < 1.0:
            rpm = cfg.idle_rpm
            load = 18.0
            throttle = 0.0
            fuel_rate = cfg.idle_fuel_lph
            self.runtime_s += dt
        else:
            # Rough gear map: rpm rises with speed, load with accel demand
            gear_ratio = 1.0 + min(4.0, speed_kmh / 40.0)
            rpm = min(cfg.redline_rpm, cfg.idle_rpm + speed_kmh * 38.0 / max(gear_ratio, 1.0) * 1.4)
            throttle = max(0.0, min(100.0, (accel / cfg.max_accel_ms2) * 85.0 + speed_kmh * 0.15))
            load = max(15.0, min(100.0, 20.0 + throttle * 0.7 + abs(min(accel, 0)) * 5.0))
            fuel_rate = cfg.idle_fuel_lph + (load / 100.0) * cfg.load_fuel_lph
            self.runtime_s += dt

        # Thermal lag toward operating targets
        if self.ignition:
            coolant_target = 88.0 + load * 0.12 + max(0.0, speed_kmh - 100) * 0.05
            oil_target = 95.0 + load * 0.18
        else:
            coolant_target = self.ambient_c
            oil_target = self.ambient_c

        self.coolant_c += (coolant_target - self.coolant_c) * min(1.0, cfg.coolant_ambient_gain * dt * 8)
        self.oil_temp_c += (oil_target - self.oil_temp_c) * min(1.0, cfg.oil_ambient_gain * dt * 8)
        self.intake_air_c = self.ambient_c + (8.0 if self.ignition else 0.0)

        if self.force_coolant_c is not None:
            self.coolant_c = self.force_coolant_c
        if self.force_oil_temp_c is not None:
            self.oil_temp_c = self.force_oil_temp_c

        # Battery model
        if self._crank_remaining_s > 0:
            self._crank_remaining_s = max(0.0, self._crank_remaining_s - dt)
            # Already dipped at crank start; hold low-ish then recover toward charging
            self.battery_v = min(self.battery_v + 0.4 * dt, 12.2)
            self.ecu_v = self.battery_v + 0.05
            self._resting = False
        elif self.ignition:
            # Alternator charging
            charge_target = 13.9 if speed_kmh > 5 else 13.6
            self.battery_v += (charge_target - self.battery_v) * min(1.0, 0.35 * dt)
            self.ecu_v = self.battery_v - 0.05
            self._resting = False
        else:
            # Resting surface charge decay toward true resting voltage
            rest_target = 12.45 if self.battery_v > 12.6 else self.battery_v
            self.battery_v += (rest_target - self.battery_v) * min(1.0, 0.05 * dt)
            self.ecu_v = self.battery_v - 0.02
            self._resting = True

        if self.force_battery_v is not None:
            self.battery_v = self.force_battery_v
        if self.force_ecu_v is not None:
            self.ecu_v = self.force_ecu_v

        # Fuel burn
        if fuel_rate > 0 and cfg.fuel_tank_l > 0:
            burned_l = fuel_rate * (dt / 3600.0)
            self.fuel_pct = max(0.0, self.fuel_pct - (burned_l / cfg.fuel_tank_l) * 100.0)

        dus = (
            self.force_distance_until_service
            if self.force_distance_until_service is not None
            else self.distance_until_service_km
        )

        green_type = green_val = None
        if self.green_event is not None:
            green_type, green_val = self.green_event
            self.green_event = None  # one-shot

        overspeed = self.overspeed_event
        self.overspeed_event = None

        return VehicleState(
            ts=ts,
            lat=self.lat,
            lon=self.lon,
            heading_deg=self.heading,
            speed_kmh=speed_kmh,
            accel_ms2=accel,
            odometer_km=self.odometer_km,
            ignition=self.ignition,
            movement=speed_kmh > 1.0,
            rpm=rpm,
            engine_load=load,
            throttle=throttle,
            coolant_c=self.coolant_c,
            oil_temp_c=self.oil_temp_c,
            intake_air_c=self.intake_air_c,
            ambient_c=self.ambient_c,
            runtime_s=self.runtime_s,
            battery_v=self.battery_v,
            ecu_v=self.ecu_v,
            tracker_battery_v=self.tracker_battery_v,
            fuel_level_pct=self.fuel_pct,
            fuel_rate_lph=fuel_rate,
            distance_until_service_km=dus,
            altitude_m=25 + int(2 * math.sin(self.odometer_km)),
            satellites=14,
            green_driving_type=green_type,
            green_driving_value=green_val,
            overspeed_kmh=overspeed,
            dtc_codes=list(self.dtc_codes),
            vin=self.vin if self.include_vin else None,
        )

    def _advance(self, dist_m: float) -> None:
        """Move along current heading; gentle curve for a continuous path."""
        self.heading = (self.heading + 0.08 * (dist_m / 10.0)) % 360.0
        brng = math.radians(self.heading)
        d = dist_m / 1000.0 / _R_KM
        lat1 = math.radians(self.lat)
        lon1 = math.radians(self.lon)
        lat2 = math.asin(
            math.sin(lat1) * math.cos(d) + math.cos(lat1) * math.sin(d) * math.cos(brng)
        )
        lon2 = lon1 + math.atan2(
            math.sin(brng) * math.sin(d) * math.cos(lat1),
            math.cos(d) - math.sin(lat1) * math.sin(lat2),
        )
        self.lat = math.degrees(lat2)
        self.lon = (math.degrees(lon2) + 540.0) % 360.0 - 180.0
