"""
Longitudinal + thermal + CAN hybrid physics for the KL Grind FMC150 sim.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from route import RouteFollower, kl_ambient_c


@dataclass
class VehicleState:
    ts: datetime
    lat: float
    lon: float
    heading_deg: float
    speed_kmh: float
    accel_ms2: float
    odometer_km: float
    ignition: bool
    movement: bool

    rpm: float
    engine_load: float
    throttle: float
    coolant_c: float
    oil_temp_c: float
    intake_air_c: float
    ambient_c: float
    runtime_s: float

    battery_v: float
    ecu_v: float
    tracker_battery_v: float
    vehicle_battery_v: float

    fuel_level_pct: float
    fuel_level_liters: float
    fuel_consumed_l: float
    fuel_rate_lph: float
    distance_until_service_km: float
    remaining_distance_km: float

    engine_hours_min: float
    engine_oil_pressure_kpa: float
    engine_oil_level_pct: float
    hv_battery_charge_pct: float

    altitude_m: int = 40
    satellites: int = 14

    green_driving_type: Optional[int] = None
    green_driving_value: Optional[int] = None
    overspeed_kmh: Optional[int] = None
    dtc_codes: list[str] = field(default_factory=list)
    vin: Optional[str] = None


@dataclass
class PhysicsConfig:
    mass_kg: float = 1450.0
    drag_cd_a: float = 0.72
    rolling_crr: float = 0.014
    max_accel_ms2: float = 3.2
    max_brake_ms2: float = 7.5
    idle_rpm: float = 800.0
    redline_rpm: float = 6200.0
    coolant_ambient_gain: float = 0.04
    oil_ambient_gain: float = 0.025
    fuel_tank_l: float = 50.0
    idle_fuel_lph: float = 0.85
    load_fuel_lph: float = 11.5


class VehiclePhysics:
    def __init__(
        self,
        *,
        odometer_km: float = 49850.0,
        distance_until_service_km: float = 450.0,
        fuel_pct: float = 100.0,
        engine_hours_min: float = 120000.0,
        ambient_c: float = 30.0,
        vin: str = "JTDBR32E720012345",
        cfg: Optional[PhysicsConfig] = None,
        route: Optional[RouteFollower] = None,
    ) -> None:
        self.cfg = cfg or PhysicsConfig()
        self.route = route or RouteFollower("morning")
        self.lat = self.route.lat
        self.lon = self.route.lon
        self.heading = self.route.heading
        self.speed_ms = 0.0
        self.odometer_km = odometer_km
        self.distance_until_service_km = distance_until_service_km
        self.fuel_pct = fuel_pct
        self.fuel_consumed_l = 0.0
        self.fuel_level_liters = (fuel_pct / 100.0) * self.cfg.fuel_tank_l
        self.engine_hours_min = engine_hours_min
        self.ambient_c = ambient_c
        self.coolant_c = ambient_c
        self.oil_temp_c = ambient_c
        self.intake_air_c = ambient_c
        self.runtime_s = 0.0
        self.ignition = False
        self.battery_v = 12.55
        self.ecu_v = 12.50
        self.tracker_battery_v = 4.05
        self.vehicle_battery_v = 12.50
        self.hv_battery_charge_pct = 45.0
        self.engine_oil_pressure_kpa = 0.0
        self.engine_oil_level_pct = 100.0
        self.remaining_distance_km = 0.0
        self.vin = vin
        self._crank_remaining_s = 0.0

        # Progressive degradation (1.0 = healthy)
        self.thermostat_health = 1.0
        self.battery_soh = 1.0
        self.cooling_efficiency = 1.0
        self.fuel_rich = 1.0
        self.accessory_load = 0.0

        # Scenario overlays
        self.force_coolant_c: Optional[float] = None
        self.force_oil_temp_c: Optional[float] = None
        self.force_battery_v: Optional[float] = None
        self.force_vehicle_battery_v: Optional[float] = None
        self.force_distance_until_service: Optional[float] = None
        self.dtc_codes: list[str] = []
        self.green_event: Optional[tuple[int, int]] = None
        self.overspeed_event: Optional[int] = None
        self.include_vin = True
        self.max_accel_override: Optional[float] = None
        self.max_brake_override: Optional[float] = None

    def set_leg(self, leg: str) -> None:
        self.route.set_leg(leg)
        self.lat = self.route.lat
        self.lon = self.route.lon
        self.heading = self.route.heading

    def set_ignition(self, on: bool, *, crank: bool = False) -> None:
        if on and not self.ignition:
            self.runtime_s = 0.0
            if crank:
                self._crank_remaining_s = 1.5
                dip = 1.35 + (1.0 - self.battery_soh) * 2.0
                self.battery_v = min(self.battery_v, 12.4) - dip
                self.vehicle_battery_v = self.battery_v
                self.ecu_v = self.battery_v + 0.05
        self.ignition = on
        if not on:
            self.speed_ms = 0.0

    def refuel(self, target_liters: Optional[float] = None) -> float:
        """Fill tank toward target liters. Does not reset fuel_consumed_l."""
        tank = self.cfg.fuel_tank_l
        if tank <= 0:
            return 0.0
        current = (self.fuel_pct / 100.0) * tank
        target = tank if target_liters is None else min(tank, max(0.0, target_liters))
        added = max(0.0, target - current)
        self.fuel_pct = (target / tank) * 100.0
        self.fuel_level_liters = target
        return added

    def tick_degradation(self, dt_h: float, thermo_rate: float, batt_rate: float) -> None:
        if thermo_rate > 0:
            self.thermostat_health = max(0.05, self.thermostat_health - thermo_rate * dt_h)
            self.cooling_efficiency = max(0.1, self.thermostat_health)
        if batt_rate > 0:
            self.battery_soh = max(0.15, self.battery_soh - batt_rate * dt_h)

    def step(
        self,
        dt: float,
        target_speed_kmh: float,
        ts: datetime,
        *,
        narrative_hour: float = 12.0,
        aggression: float = 0.0,
    ) -> VehicleState:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        cfg = self.cfg
        self.ambient_c = kl_ambient_c(narrative_hour)
        max_accel = self.max_accel_override or cfg.max_accel_ms2
        max_brake = self.max_brake_override or cfg.max_brake_ms2
        if aggression > 0.5:
            max_accel *= 1.0 + 0.4 * aggression
            max_brake *= 1.0 + 0.5 * aggression

        target_ms = max(0.0, target_speed_kmh) / 3.6
        if not self.ignition:
            target_ms = 0.0

        dv = target_ms - self.speed_ms
        if dv >= 0:
            accel = min(max_accel, dv / max(dt, 1e-3))
        else:
            accel = max(-max_brake, dv / max(dt, 1e-3))

        if self.speed_ms > 0.1:
            drag = (0.5 * 1.2 * cfg.drag_cd_a * self.speed_ms * self.speed_ms) / cfg.mass_kg
            roll = cfg.rolling_crr * 9.81
            if accel > 0:
                accel = max(0.0, accel - drag - roll)

        self.speed_ms = max(0.0, self.speed_ms + accel * dt)
        if abs(self.speed_ms - target_ms) < 0.05:
            self.speed_ms = target_ms

        dist_m = self.speed_ms * dt
        if dist_m > 0:
            self.route.advance(dist_m)
            self.lat = self.route.lat
            self.lon = self.route.lon
            self.heading = self.route.heading
            self.odometer_km += dist_m / 1000.0
            if self.force_distance_until_service is None:
                self.distance_until_service_km = max(
                    0.0, self.distance_until_service_km - dist_m / 1000.0
                )

        speed_kmh = self.speed_ms * 3.6
        if not self.ignition:
            rpm = load = throttle = fuel_rate = 0.0
        elif speed_kmh < 1.0:
            rpm = cfg.idle_rpm + self.accessory_load * 80.0
            load = 18.0 + self.accessory_load * 25.0
            throttle = 0.0
            fuel_rate = (cfg.idle_fuel_lph + self.accessory_load * 0.6) * self.fuel_rich
            self.runtime_s += dt
            self.engine_hours_min += dt / 60.0
        else:
            gear_ratio = 1.0 + min(4.0, speed_kmh / 40.0)
            rpm = min(
                cfg.redline_rpm,
                cfg.idle_rpm + speed_kmh * 38.0 / max(gear_ratio, 1.0) * 1.4,
            )
            if aggression > 0.6:
                rpm = min(cfg.redline_rpm, rpm * (1.0 + 0.25 * aggression))
            throttle = max(0.0, min(100.0, (accel / max_accel) * 85.0 + speed_kmh * 0.15))
            load = max(15.0, min(100.0, 20.0 + throttle * 0.7 + abs(min(accel, 0)) * 5.0))
            fuel_rate = (cfg.idle_fuel_lph + (load / 100.0) * cfg.load_fuel_lph) * self.fuel_rich
            self.runtime_s += dt
            self.engine_hours_min += dt / 60.0

        # Thermal — degraded thermostat raises target and slows cooling
        cool_eff = max(0.1, self.cooling_efficiency)
        if self.ignition:
            base_cool = 88.0 + load * 0.12 + max(0.0, speed_kmh - 100) * 0.05
            idle_penalty = 8.0 * self.accessory_load if speed_kmh < 2 else 0.0
            stuck = (1.0 - cool_eff) * 28.0
            coolant_target = base_cool + idle_penalty + stuck
            oil_target = 95.0 + load * 0.18 + stuck * 0.7
            gain_mul = 0.5 + 1.5 * (1.0 - cool_eff)
        else:
            coolant_target = self.ambient_c
            oil_target = self.ambient_c
            gain_mul = 1.0

        self.coolant_c += (coolant_target - self.coolant_c) * min(
            1.0, cfg.coolant_ambient_gain * dt * 8 * gain_mul
        )
        self.oil_temp_c += (oil_target - self.oil_temp_c) * min(
            1.0, cfg.oil_ambient_gain * dt * 8 * gain_mul
        )
        self.intake_air_c = self.ambient_c + (8.0 if self.ignition else 0.0)

        if self.force_coolant_c is not None:
            self.coolant_c = self.force_coolant_c
        if self.force_oil_temp_c is not None:
            self.oil_temp_c = self.force_oil_temp_c

        # Electrical
        soh = self.battery_soh
        if self._crank_remaining_s > 0:
            self._crank_remaining_s = max(0.0, self._crank_remaining_s - dt)
            self.battery_v = min(self.battery_v + 0.4 * dt, 11.8 + soh * 0.4)
            self.vehicle_battery_v = self.battery_v
            self.ecu_v = self.battery_v + 0.05
        elif self.ignition:
            charge_target = (13.9 if speed_kmh > 5 else 13.5) * (0.92 + 0.08 * soh)
            charge_target -= self.accessory_load * 0.35
            self.battery_v += (charge_target - self.battery_v) * min(1.0, 0.35 * dt)
            self.vehicle_battery_v = self.battery_v - 0.08
            self.ecu_v = self.battery_v - 0.05
        else:
            rest_target = 12.55 * soh + 11.2 * (1.0 - soh)
            self.battery_v += (rest_target - self.battery_v) * min(1.0, 0.05 * dt)
            self.vehicle_battery_v = self.battery_v - 0.05
            self.ecu_v = self.battery_v - 0.02

        if self.force_battery_v is not None:
            self.battery_v = self.force_battery_v
            self.ecu_v = self.force_battery_v + 0.02
        if self.force_vehicle_battery_v is not None:
            self.vehicle_battery_v = self.force_vehicle_battery_v

        # HV hybrid pack
        if self.ignition:
            if accel < -1.0:
                self.hv_battery_charge_pct = min(100.0, self.hv_battery_charge_pct + 0.4 * dt)
            elif speed_kmh > 50:
                self.hv_battery_charge_pct = max(5.0, self.hv_battery_charge_pct - 0.04 * dt)
            else:
                self.hv_battery_charge_pct = max(5.0, self.hv_battery_charge_pct - 0.01 * dt)

        # Oil pressure / level
        if self.ignition:
            self.engine_oil_pressure_kpa = 280.0 + load * 1.6 + max(0.0, speed_kmh - 40) * 0.4
            self.engine_oil_level_pct = max(70.0, 100.0 - (1.0 - self.thermostat_health) * 5.0)
        else:
            self.engine_oil_pressure_kpa = 0.0

        # Fuel burn
        if fuel_rate > 0 and cfg.fuel_tank_l > 0:
            burned_l = fuel_rate * (dt / 3600.0)
            self.fuel_pct = max(0.0, self.fuel_pct - (burned_l / cfg.fuel_tank_l) * 100.0)
            self.fuel_consumed_l += burned_l
        self.fuel_level_liters = max(0.0, (self.fuel_pct / 100.0) * cfg.fuel_tank_l)

        if speed_kmh > 5 and fuel_rate > 0.15:
            l_per_100 = (fuel_rate / speed_kmh) * 100.0
            self.remaining_distance_km = self.fuel_level_liters / max(l_per_100 / 100.0, 1e-3)
        else:
            self.remaining_distance_km = self.fuel_level_liters * 14.0

        dus = (
            self.force_distance_until_service
            if self.force_distance_until_service is not None
            else self.distance_until_service_km
        )

        green_type = green_val = None
        if self.green_event is not None:
            green_type, green_val = self.green_event
            self.green_event = None

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
            vehicle_battery_v=self.vehicle_battery_v,
            fuel_level_pct=self.fuel_pct,
            fuel_level_liters=self.fuel_level_liters,
            fuel_consumed_l=self.fuel_consumed_l,
            fuel_rate_lph=fuel_rate,
            distance_until_service_km=dus,
            remaining_distance_km=self.remaining_distance_km,
            engine_hours_min=self.engine_hours_min,
            engine_oil_pressure_kpa=self.engine_oil_pressure_kpa,
            engine_oil_level_pct=self.engine_oil_level_pct,
            hv_battery_charge_pct=self.hv_battery_charge_pct,
            altitude_m=int(round(self.route.alt_m)),
            satellites=14,
            green_driving_type=green_type,
            green_driving_value=green_val,
            overspeed_kmh=overspeed,
            dtc_codes=list(self.dtc_codes),
            vin=self.vin if self.include_vin else None,
        )

    def checkpoint(self) -> dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "heading": self.heading,
            "speed_ms": self.speed_ms,
            "odometer_km": self.odometer_km,
            "distance_until_service_km": self.distance_until_service_km,
            "fuel_pct": self.fuel_pct,
            "fuel_consumed_l": self.fuel_consumed_l,
            "engine_hours_min": self.engine_hours_min,
            "coolant_c": self.coolant_c,
            "oil_temp_c": self.oil_temp_c,
            "battery_v": self.battery_v,
            "vehicle_battery_v": self.vehicle_battery_v,
            "hv_battery_charge_pct": self.hv_battery_charge_pct,
            "thermostat_health": self.thermostat_health,
            "battery_soh": self.battery_soh,
            "cooling_efficiency": self.cooling_efficiency,
            "ignition": self.ignition,
        }

    def restore(self, data: dict[str, Any]) -> None:
        for k, v in data.items():
            if hasattr(self, k):
                setattr(self, k, v)
        self.fuel_level_liters = (self.fuel_pct / 100.0) * self.cfg.fuel_tank_l
        self.route.lat = self.lat
        self.route.lon = self.lon
        self.route.heading = self.heading
