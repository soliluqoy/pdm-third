"""
End-to-end scenario: one continuous trip (plus park/rest) that exercises
threshold alerts, DTCs, driving behavior, scheduled service, and PME signals.

Everything talks to a running PREDICT stack over HTTP (:8000) + TCP (:5123).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from api import PredictApi
from avl_map import state_to_record
from physics import VehiclePhysics
from tcp_client import TeltonikaClient

log = logging.getLogger("sim.scenario")

# Default sim car — unique IMEI so it won't collide with a real tracker.
DEFAULT_CAR = {
    "name": "Simulation Corolla",
    "imei": "359633090000001",
    "device_type": "fmc001",
    "license_plate": "SIM-001",
    "make": "Toyota",
    "model": "Corolla",
    "year": 2019,
    "vin": "JTDBR32E720012345",
    "mass_kg": 1400,
    "oil_capacity_l": 4.4,
    # Small pad budget so brake PME score drops within this trip.
    "brake_pad_capacity_mj": 2.5,
    "last_oil_change_odo": 40000,
    "last_brake_service_odo": 47000,
}


@dataclass
class PhaseResult:
    name: str
    seconds: float
    notes: str = ""


class Scenario:
    def __init__(
        self,
        api: PredictApi,
        client: TeltonikaClient,
        physics: VehiclePhysics,
        *,
        sample_interval: float = 2.0,
        idle_minutes: float = 5.0,
        on_tick: Optional[Callable[[str, VehiclePhysics], None]] = None,
    ) -> None:
        self.api = api
        self.client = client
        self.physics = physics
        self.dt = sample_interval
        self.idle_minutes = idle_minutes
        self.on_tick = on_tick
        self.vehicle_id: Optional[int] = None
        self.phases: list[PhaseResult] = []
        self._sim_t0 = time.monotonic()

    # ── helpers ──────────────────────────────────────────────────────────────
    def _now(self) -> datetime:
        # Wall-clock timestamps so RULE_MAX_RECORD_AGE_SECONDS stays happy.
        return datetime.now(timezone.utc)

    def _emit(self, target_speed_kmh: float) -> None:
        state = self.physics.step(self.dt, target_speed_kmh, self._now())
        self.client.send_one(state_to_record(state))
        if self.on_tick:
            self.on_tick(
                f"v={state.speed_kmh:5.1f} cool={state.coolant_c:5.1f} "
                f"batt={state.battery_v:4.2f} odo={state.odometer_km:.1f}",
                self.physics,
            )

    def _hold(
        self,
        seconds: float,
        target_speed_kmh: float,
        *,
        label: str = "",
        each: Optional[Callable[[], None]] = None,
    ) -> None:
        end = time.monotonic() + seconds
        n = 0
        while time.monotonic() < end:
            if each:
                each()
            self._emit(target_speed_kmh)
            n += 1
            # Sleep the remainder of the sample interval (send time eats a bit).
            time.sleep(max(0.0, self.dt - 0.05))
        if label:
            log.info("  ... %s (%d samples, %.0fs)", label, n, seconds)

    def _phase(self, name: str, notes: str = "") -> None:
        elapsed = time.monotonic() - self._sim_t0
        log.info("")
        log.info("== Phase: %s  (t=+%.0fs) %s", name, elapsed, notes)
        self.phases.append(PhaseResult(name=name, seconds=elapsed, notes=notes))

    # ── main flow ────────────────────────────────────────────────────────────
    def _bootstrap(self) -> None:
        self._phase("register", "POST /api/v1/cars")
        car = self.api.register_car(DEFAULT_CAR)
        self.vehicle_id = int(car["id"])
        log.info("Car id=%s IMEI=%s", self.vehicle_id, car["imei"])

        # Keep ask_me_first on so WOs appear as SUGGESTED (default product path).
        try:
            self.api.patch_settings({"ask_me_first": "true"})
        except RuntimeError as e:
            log.warning("Could not patch settings: %s", e)

        self.client.connect()

    def run_smoke(self) -> dict:
        """~30 s connectivity check: register, crank, short drive, disconnect."""
        self._bootstrap()
        self._phase("smoke_drive", "crank + 20 s urban drive")
        self.physics.set_ignition(False)
        self._hold(2.0, 0.0, label="parked")
        self.physics.set_ignition(True, crank=True)
        self._hold(4.0, 0.0, label="crank")
        for spd in (20, 40, 55, 40):
            self._hold(4.0, spd, label=f"{spd} km/h")
        self.physics.set_ignition(False)
        self._hold(2.0, 0.0, label="ign off")
        self._phase("done", "smoke complete")
        time.sleep(1.0)
        return self.summary()

    def run(self) -> dict:
        self._bootstrap()

        # ── 1. Parked → crank → idle warm-up ────────────────────────────────
        self._phase("crank_and_warmup", "battery crank dip + thermal rise")
        self.physics.set_ignition(False)
        self._hold(4.0, 0.0, label="parked ign-off")
        self.physics.set_ignition(True, crank=True)
        self._hold(8.0, 0.0, label="cranking / idle")

        # ── 2. Pull away, urban drive ───────────────────────────────────────
        self._phase("urban_drive", "accelerate through city speeds")
        for spd in (15, 30, 45, 55):
            self._hold(8.0, spd, label=f"target {spd} km/h")

        # ── 3. Harsh braking burst (behavior ≥ 8 + brake PME energy) ────────
        self._phase("harsh_brakes", "8+ device harsh_brake events + physics stops")
        for i in range(10):
            # Build speed then slam brakes (physics decel + device event)
            self._hold(4.0, 85.0, label=f"build speed #{i+1}")
            self.physics.green_event = (2, 25 + i)  # type 2 = harsh_brake
            # One hard stop sample: target 0 → large negative accel
            self._emit(0.0)
            time.sleep(self.dt)
            self._hold(3.0, 0.0, label=f"stopped #{i+1}")

        # ── 4. Harsh accel + corner + speeding + high RPM ───────────────────
        self._phase("behavior_mix", "accel / corner / speeding / high RPM")
        self.physics.green_event = (1, 30)
        self._hold(4.0, 70.0, label="harsh accel overlay")
        self.physics.green_event = (3, 20)
        self._hold(4.0, 60.0, label="harsh corner overlay")
        # Speeding: need 2 consecutive samples over behavior.speed_limit_kmh (120)
        self.physics.overspeed_event = 132
        self._hold(8.0, 128.0, label="speeding 128 km/h")
        # High RPM while moving — physics will push rpm with speed; force via
        # staying at high speed briefly (threshold default 4000).
        self._hold(6.0, 110.0, label="high speed / RPM")

        # ── 5. Overheat (coolant > 110 for ≥ 120 s) ─────────────────────────
        self._phase("overheat", "coolant 115 °C held 130 s → critical alert")
        self.physics.force_coolant_c = 115.0
        self._hold(130.0, 40.0, label="overheat hold")
        self.physics.force_coolant_c = None

        # ── 6. Oil temp high (oil > 130 for ≥ 300 s) — overlaps cruise ──────
        self._phase("oil_temp_high", "oil 136 °C held 310 s → warning alert")
        self.physics.force_oil_temp_c = 136.0
        self._hold(310.0, 50.0, label="oil over-temp hold")
        self.physics.force_oil_temp_c = None

        # ── 7. Service due soon (distance_until_service < 500) ──────────────
        self._phase("service_due", "distance_until_service = 120 km")
        self.physics.force_distance_until_service = 120.0
        self._hold(8.0, 40.0, label="service countdown")

        # ── 8. DTC ──────────────────────────────────────────────────────────
        self._phase("dtc", "P0300 + P0128 fault codes")
        self.physics.dtc_codes = ["P0300", "P0128"]
        self._hold(8.0, 35.0, label="DTC broadcast")
        self.physics.dtc_codes = []

        # ── 9. Idling (default 5 min near-zero speed) ───────────────────────
        idle_s = max(60.0, self.idle_minutes * 60.0 + 15.0)
        self._phase("idling", f"hold ~0 km/h for {idle_s:.0f}s")
        self._hold(idle_s, 0.0, label="idle")

        # ── 10. Scheduled service interval: push odometer past next_due ─────
        # First odometer sighting earlier set next_due ≈ start_odo + 10000.
        # Jump odometer by sending a high reading (still continuous trip).
        self._phase("scheduled_service", "odometer past 10 000 km interval")
        # Anchor was ~49850 on first packets → due ~59850. Jump past it.
        self.physics.odometer_km = max(self.physics.odometer_km, 59950.0)
        self._hold(10.0, 45.0, label="odometer past interval")

        # ── 11. Coolant hot warning band (105–110) if not already critical ──
        self._phase("coolant_hot", "coolant 107 °C held 310 s → warning")
        self.physics.force_coolant_c = 107.0
        self._hold(310.0, 40.0, label="coolant_hot hold")
        self.physics.force_coolant_c = None

        # ── 12. End trip (ignition off) → behavior rule + brake PME ─────────
        self._phase("trip_end", "ignition off closes trip")
        self.physics.set_ignition(False)
        self._hold(6.0, 0.0, label="ignition off")

        # ── 13. Weak resting battery + low ECU voltage ──────────────────────
        self._phase("battery_low", "resting batt 11.4 V / ECU 11.5 V")
        self.physics.force_battery_v = 11.4
        self.physics.force_ecu_v = 11.5
        self._hold(130.0, 0.0, label="low voltage hold (ign off)")
        # Also cover battery_low (<11.8 for 60s) — already past that.

        # ── 14. Short trip loop to worsen battery / oil PME ─────────────────
        self._phase("short_trips", "3 short trips for battery/oil PME pressure")
        self.physics.force_battery_v = None
        self.physics.force_ecu_v = None
        for i in range(3):
            self.physics.set_ignition(True, crank=True)
            # Keep resting voltage depressed between trips for battery score
            if i > 0:
                self.physics.battery_v = 11.9
                self.physics.ecu_v = 11.85
            self._hold(4.0, 0.0, label=f"short trip {i+1} crank")
            self._hold(25.0, 40.0, label=f"short trip {i+1} drive")
            self.physics.set_ignition(False)
            self._hold(8.0, 0.0, label=f"short trip {i+1} park")
            # Depress resting voltage after each short trip
            self.physics.force_battery_v = 11.85
            self.physics.force_ecu_v = 11.80
            self._hold(20.0, 0.0, label=f"short trip {i+1} rest")
            self.physics.force_battery_v = None
            self.physics.force_ecu_v = None

        # Final resting sample so predictor sees low median resting V
        self.physics.force_battery_v = 11.75
        self.physics.force_ecu_v = 11.70
        self._hold(30.0, 0.0, label="final weak rest")
        self.physics.force_battery_v = None
        self.physics.force_ecu_v = None

        self._phase("done", "collecting API summary")
        time.sleep(2.0)  # let async PME / WS settle
        return self.summary()

    def summary(self) -> dict:
        vid = self.vehicle_id
        overview = None
        prognostics = None
        alerts: list = []
        workorders: list = []
        trips: list = []
        events: list = []
        try:
            overview = self.api.overview()
        except RuntimeError as e:
            log.warning("overview: %s", e)
        if vid is not None:
            try:
                prognostics = self.api.car_prognostics(vid)
            except RuntimeError as e:
                log.warning("prognostics: %s", e)
            try:
                alerts = self.api.alerts(vid)
            except RuntimeError as e:
                log.warning("alerts: %s", e)
            try:
                workorders = self.api.workorders(vid)
            except RuntimeError as e:
                log.warning("workorders: %s", e)
            trips = self.api.trips(vid)
            events = self.api.driving_events(vid)

        active = [a for a in alerts if str(a.get("status", "")).lower() == "active"]
        return {
            "vehicle_id": vid,
            "imei": DEFAULT_CAR["imei"],
            "phases": [p.name for p in self.phases],
            "elapsed_s": time.monotonic() - self._sim_t0,
            "alerts_total": len(alerts),
            "alerts_active": len(active),
            "alert_titles": sorted({a.get("title") or a.get("message", "?") for a in alerts}),
            "workorders": len(workorders),
            "workorder_titles": [w.get("title") for w in workorders],
            "trips": len(trips),
            "driving_events": len(events),
            "event_types": sorted({e.get("event_type") for e in events if e.get("event_type")}),
            "prognostics": prognostics,
            "overview_cars": (
                len(overview) if isinstance(overview, list)
                else len((overview or {}).get("cars", []))
                if isinstance(overview, dict) else None
            ),
        }
