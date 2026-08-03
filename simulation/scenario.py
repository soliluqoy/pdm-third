"""
KL Grind: real wall-clock FMC150 taxi-shift scenario for PREDICT.
"""
from __future__ import annotations

import json
import logging
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from api import PredictApi
from avl_map import state_to_record
from physics import VehiclePhysics
from route import narrative_hour
from schedule import (
    DEV_DURATION_HOURS,
    DEV_PHASES,
    FULL_PHASES,
    Phase,
    phase_at,
    scaled_phases,
)
from tcp_client import TeltonikaClient

log = logging.getLogger("sim.scenario")

DEFAULT_CAR = {
    "name": "KL Hybrid Taxi (SIM)",
    "imei": "359633090000001",
    "device_type": "fmc150",
    "license_plate": "SIM-KL1",
    "make": "Toyota",
    "model": "Corolla Hybrid (CAN)",
    "year": 2019,
    "vin": "JTDBR32E720012345",
    "mass_kg": 1450,
    "oil_capacity_l": 4.4,
    "brake_pad_capacity_mj": 2.5,
    "last_oil_change_odo": 40000,
    "last_brake_service_odo": 47000,
}

CHECKPOINT_PATH = Path(__file__).resolve().parent / ".kl_grind_checkpoint.json"


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
        sample_interval: float = 5.0,
        duration_hours: float = 24.0,
        phases: Optional[list[Phase]] = None,
        checkpoint_path: Path = CHECKPOINT_PATH,
        resume: bool = False,
        on_tick: Optional[Callable[[str, VehiclePhysics], None]] = None,
    ) -> None:
        self.api = api
        self.client = client
        self.physics = physics
        self.dt = sample_interval
        self.duration_hours = duration_hours
        self.phases_table = phases or FULL_PHASES
        self.checkpoint_path = checkpoint_path
        self.resume = resume
        self.on_tick = on_tick
        self.vehicle_id: Optional[int] = None
        self.phase_log: list[PhaseResult] = []
        self.packets_sent = 0
        self._t0_wall: Optional[float] = None
        self._t0_mono: Optional[float] = None
        self._elapsed_offset_s = 0.0
        self._stop = False
        self._current_key: Optional[str] = None
        self._done_flags: set[str] = set()
        self._harsh_emitted = 0
        self._harsh_step = 0
        self._behavior_done = False
        self._refueled = False
        self._jumped_odo = False
        self._jumped_hours = False
        self._limp_i = 0

    def request_stop(self, *_args) -> None:
        log.warning("Stop requested - finishing current sample and checkpointing")
        self._stop = True

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _elapsed_s(self) -> float:
        assert self._t0_mono is not None
        return self._elapsed_offset_s + (time.monotonic() - self._t0_mono)

    def _emit(self, target_speed_kmh: float, aggression: float = 0.0) -> None:
        elapsed = self._elapsed_s()
        state = self.physics.step(
            self.dt,
            target_speed_kmh,
            self._now(),
            narrative_hour=narrative_hour(elapsed),
            aggression=aggression,
        )
        try:
            self.client.send_one(state_to_record(state))
            self.packets_sent += 1
        except RuntimeError as e:
            log.error("TCP send failed after retries: %s", e)
            raise
        if self.on_tick:
            self.on_tick(
                f"v={state.speed_kmh:5.1f} cool={state.coolant_c:5.1f} "
                f"vbatt={state.vehicle_battery_v:4.1f} fuel={state.fuel_consumed_l:5.2f}L "
                f"odo={state.odometer_km:.1f}",
                self.physics,
            )

    def _sleep_interval(self) -> None:
        time.sleep(max(0.0, self.dt - 0.02))

    def _bootstrap(self) -> None:
        log.info("== Phase: register")
        car = self.api.register_car(DEFAULT_CAR)
        self.vehicle_id = int(car["id"])
        log.info(
            "Car id=%s IMEI=%s device_type=%s",
            self.vehicle_id, car["imei"], car.get("device_type"),
        )
        try:
            self.api.patch_settings({"ask_me_first": "true"})
        except RuntimeError as e:
            log.warning("Could not patch settings: %s", e)
        self.client.connect()

    def _apply_phase_enter(self, phase: Phase) -> None:
        log.info("")
        log.info(
            "== Phase: %s  (elapsed=%.2fh) %s",
            phase.key, self._elapsed_s() / 3600.0, phase.label,
        )
        self.phase_log.append(
            PhaseResult(name=phase.key, seconds=self._elapsed_s(), notes=phase.label)
        )
        self.physics.set_leg(phase.leg)
        self.physics.accessory_load = phase.accessory_load
        self.physics.fuel_rich = phase.fuel_rich
        self.physics.dtc_codes = list(phase.dtc)

        if phase.trip_break and f"break:{phase.key}" not in self._done_flags:
            self._done_flags.add(f"break:{phase.key}")
            if self.physics.ignition:
                self.physics.set_ignition(False)
                for _ in range(max(2, int(8 / self.dt))):
                    self._emit(0.0)
                    self._sleep_interval()

        if phase.dead:
            self.physics.set_ignition(False)
        elif phase.ignition and not self.physics.ignition:
            self.physics.set_ignition(True, crank=True)

        if phase.refuel and not self._refueled:
            added = self.physics.refuel(self.physics.cfg.fuel_tank_l)
            self._refueled = True
            log.info("Refueled +%.1f L -> tank %.0f%% (fuel_consumed unchanged=%.2f)",
                     added, self.physics.fuel_pct, self.physics.fuel_consumed_l)

        if phase.jump_odo is not None and not self._jumped_odo:
            self.physics.odometer_km = max(self.physics.odometer_km, phase.jump_odo)
            self._jumped_odo = True
            log.info("Odometer jumped to %.1f km", self.physics.odometer_km)

        if phase.jump_engine_hours_min is not None and not self._jumped_hours:
            self.physics.engine_hours_min = max(
                self.physics.engine_hours_min, phase.jump_engine_hours_min
            )
            self._jumped_hours = True
            log.info("Engine hours jumped to %.0f min", self.physics.engine_hours_min)

        if phase.service_distance is not None:
            self.physics.force_distance_until_service = phase.service_distance

        self._harsh_emitted = 0
        self._harsh_step = 0
        self._behavior_done = False

    def _apply_phase_tick(self, phase: Phase) -> float:
        """Update overlays; return target speed for this sample."""
        self.physics.force_coolant_c = phase.force_coolant
        self.physics.force_oil_temp_c = phase.force_oil
        if phase.force_tracker_batt is not None:
            self.physics.force_battery_v = phase.force_tracker_batt
        else:
            self.physics.force_battery_v = None
        if phase.force_vehicle_batt is not None:
            # AVL 168 is integer volts — use 11 to fire car_battery_low (<12)
            v = phase.force_vehicle_batt
            self.physics.force_vehicle_battery_v = 11.0 if v < 12.0 else v
        else:
            self.physics.force_vehicle_battery_v = None

        self.physics.tick_degradation(
            self.dt / 3600.0, phase.degrade_thermo, phase.degrade_battery
        )

        # Limp: cycle short trips
        if phase.key == "limp":
            cycle = int(self._elapsed_s()) // 90
            if cycle != self._limp_i:
                self._limp_i = cycle
                if self.physics.ignition:
                    self.physics.set_ignition(False)
                else:
                    self.physics.battery_v = 11.3
                    self.physics.set_ignition(True, crank=True)

        if phase.dead:
            self.physics.set_ignition(False)
            return 0.0

        if phase.idle:
            return 0.0

        # Harsh brake bursts early in aggressive phases (build → slam)
        if phase.harsh_brakes_burst and self._harsh_emitted < phase.harsh_brakes_burst:
            self._harsh_step += 1
            if self._harsh_step % 4 != 0:
                return 85.0
            self.physics.green_event = (2, 25 + self._harsh_emitted)
            self._harsh_emitted += 1
            return 0.0

        if (
            phase.behavior_mix
            and not self._behavior_done
            and self._harsh_emitted >= phase.harsh_brakes_burst
        ):
            self._behavior_done = True
            self.physics.green_event = (1, 30)
            self.physics.overspeed_event = 132
            return 128.0

        # Gentle speed variation for long phases
        wobble = 8.0 * math_sin_elapsed(self._elapsed_s())
        return max(0.0, phase.target_speed + wobble)

    def _heartbeat(self, phase: Phase) -> None:
        p = self.physics
        log.info(
            "heartbeat elapsed=%.2fh phase=%s odo=%.1f fuel_used=%.2fL "
            "cool=%.1f vbatt=%.1f thermo=%.2f soh=%.2f packets=%d reconnects=%d",
            self._elapsed_s() / 3600.0,
            phase.key,
            p.odometer_km,
            p.fuel_consumed_l,
            p.coolant_c,
            p.vehicle_battery_v,
            p.thermostat_health,
            p.battery_soh,
            self.packets_sent,
            self.client.reconnects,
        )

    def _save_checkpoint(self) -> None:
        if self._t0_wall is None:
            return
        payload = {
            "t0_wall": self._t0_wall,
            "elapsed_s": self._elapsed_s(),
            "duration_hours": self.duration_hours,
            "vehicle_id": self.vehicle_id,
            "packets_sent": self.packets_sent,
            "done_flags": sorted(self._done_flags),
            "refueled": self._refueled,
            "jumped_odo": self._jumped_odo,
            "jumped_hours": self._jumped_hours,
            "physics": self.physics.checkpoint(),
            "current_key": self._current_key,
        }
        self.checkpoint_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log.info("Checkpoint written %s (elapsed=%.2fh)", self.checkpoint_path, payload["elapsed_s"] / 3600.0)

    def _load_checkpoint(self) -> Optional[dict]:
        if not self.checkpoint_path.exists():
            return None
        try:
            return json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Bad checkpoint: %s", e)
            return None

    def run_smoke(self) -> dict:
        self._bootstrap()
        self._t0_mono = time.monotonic()
        self._t0_wall = time.time()
        self._elapsed_offset_s = 0.0
        self.physics.set_leg("smoke")
        self.physics.set_ignition(False)
        self._emit(0.0)
        self._sleep_interval()
        self.physics.set_ignition(True, crank=True)
        for spd in (0, 20, 40, 55, 40, 0):
            if spd == 0 and self.physics.ignition and self.packets_sent > 3:
                pass
            self._emit(float(spd))
            self._sleep_interval()
        self.physics.set_ignition(False)
        self._emit(0.0)
        self.phase_log.append(PhaseResult("smoke", self._elapsed_s(), "connectivity"))
        time.sleep(1.0)
        return self.summary()

    def run(self) -> dict:
        self._bootstrap()

        prev_handler = signal.signal(signal.SIGINT, self.request_stop)
        try:
            ckpt = self._load_checkpoint() if self.resume else None
            if ckpt and abs(float(ckpt.get("duration_hours", -1)) - self.duration_hours) < 1e-6:
                self._elapsed_offset_s = float(ckpt["elapsed_s"])
                self._t0_wall = float(ckpt["t0_wall"])
                self._t0_mono = time.monotonic()
                self.packets_sent = int(ckpt.get("packets_sent", 0))
                self._done_flags = set(ckpt.get("done_flags") or [])
                self._refueled = bool(ckpt.get("refueled"))
                self._jumped_odo = bool(ckpt.get("jumped_odo"))
                self._jumped_hours = bool(ckpt.get("jumped_hours"))
                self.physics.restore(ckpt.get("physics") or {})
                self._current_key = ckpt.get("current_key")
                log.info("Resumed at elapsed=%.2fh", self._elapsed_offset_s / 3600.0)
            else:
                self._elapsed_offset_s = 0.0
                self._t0_wall = time.time()
                self._t0_mono = time.monotonic()
                # Parked → crank
                self.physics.set_ignition(False)
                self._emit(0.0)
                self._sleep_interval()
                self.physics.set_ignition(True, crank=True)

            end_s = self.duration_hours * 3600.0
            last_hb = 0.0
            last_ckpt = 0.0

            while not self._stop and self._elapsed_s() < end_s:
                elapsed_h = self._elapsed_s() / 3600.0
                phase = phase_at(elapsed_h, self.phases_table)
                if phase.key != self._current_key:
                    self._apply_phase_enter(phase)
                    self._current_key = phase.key

                target = self._apply_phase_tick(phase)
                self._emit(target, aggression=phase.aggression)
                self._sleep_interval()

                e = self._elapsed_s()
                if e - last_hb >= 300.0:
                    self._heartbeat(phase)
                    last_hb = e
                if e - last_ckpt >= 600.0:
                    self._save_checkpoint()
                    last_ckpt = e

            if self._stop:
                self._save_checkpoint()
            elif self.checkpoint_path.exists():
                try:
                    self.checkpoint_path.unlink()
                except OSError:
                    pass

            # Ensure ignition off at end for trip close / PME
            if self.physics.ignition:
                self.physics.set_ignition(False)
                for _ in range(3):
                    self._emit(0.0)
                    self._sleep_interval()

            self.phase_log.append(
                PhaseResult("done", self._elapsed_s(), "complete" if not self._stop else "interrupted")
            )
            time.sleep(2.0)
            return self.summary()
        finally:
            signal.signal(signal.SIGINT, prev_handler)

    def summary(self) -> dict:
        vid = self.vehicle_id
        overview = None
        prognostics = None
        vitals = None
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
                vitals = self.api.car_vitals(vid)
            except RuntimeError as e:
                log.warning("vitals: %s", e)
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

        car = None
        try:
            car = self.api.find_car_by_imei(DEFAULT_CAR["imei"])
        except RuntimeError:
            pass

        active = [a for a in alerts if str(a.get("status", "")).lower() == "active"]
        return {
            "vehicle_id": vid,
            "imei": DEFAULT_CAR["imei"],
            "device_type": (car or {}).get("device_type"),
            "phases": [p.name for p in self.phase_log],
            "elapsed_s": self._elapsed_s() if self._t0_mono else 0.0,
            "packets_sent": self.packets_sent,
            "reconnects": self.client.reconnects,
            "alerts_total": len(alerts),
            "alerts_active": len(active),
            "alerts": alerts,
            "alert_titles": sorted({a.get("title") or a.get("message", "?") for a in alerts}),
            "alert_keys": sorted({a.get("rule_key") or a.get("key") or "" for a in alerts} - {""}),
            "workorders": len(workorders),
            "workorder_titles": [w.get("title") for w in workorders],
            "trips": trips,
            "trips_count": len(trips),
            "driving_events": events,
            "driving_events_count": len(events),
            "event_types": sorted({e.get("event_type") for e in events if e.get("event_type")}),
            "prognostics": prognostics,
            "vitals": vitals,
            "overview_cars": (
                len(overview) if isinstance(overview, list)
                else len((overview or {}).get("cars", []))
                if isinstance(overview, dict) else None
            ),
        }


def math_sin_elapsed(elapsed_s: float) -> float:
    import math
    return math.sin(elapsed_s / 40.0)


def build_phases(mode: str, duration_hours: float) -> tuple[list[Phase], float]:
    if mode == "dev":
        return DEV_PHASES, DEV_DURATION_HOURS
    if abs(duration_hours - 24.0) < 1e-9:
        return FULL_PHASES, 24.0
    return scaled_phases(duration_hours), duration_hours
