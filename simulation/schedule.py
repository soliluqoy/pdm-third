"""
24-hour KL Grind phase timetable (elapsed wall hours from scenario start).

Narrative labels assume a taxi day starting at 07:00 local KL; the runner
uses elapsed wall time, not wall-clock local hour.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Phase:
    key: str
    label: str
    start_h: float
    end_h: float
    leg: str
    # Driving intent
    target_speed: float = 0.0
    ignition: bool = True
    aggression: float = 0.0          # 0..1 — raises accel/brake events
    idle: bool = False
    accessory_load: float = 0.0      # electrical / AC load
    refuel: bool = False
    force_coolant: Optional[float] = None
    force_oil: Optional[float] = None
    force_vehicle_batt: Optional[float] = None
    force_tracker_batt: Optional[float] = None
    dtc: tuple[str, ...] = ()
    jump_odo: Optional[float] = None
    jump_engine_hours_min: Optional[float] = None
    service_distance: Optional[float] = None
    degrade_thermo: float = 0.0      # per sim-hour decay of thermostat_health
    degrade_battery: float = 0.0     # per sim-hour decay of battery_soh
    fuel_rich: float = 1.0           # multiplies fuel burn
    trip_break: bool = False         # request ignition-off trip boundary at phase start
    harsh_brakes_burst: int = 0      # emit N device harsh_brake events during phase
    behavior_mix: bool = False       # accel/corner/speeding/high rpm once
    dead: bool = False               # final no-start


# Full 24h taxi shift (elapsed hours)
FULL_PHASES: list[Phase] = [
    Phase(
        "morning", "Morning rush PJ->KLCC (07:00-10:00)",
        0.0, 3.0, "morning", target_speed=55.0, aggression=0.1,
    ),
    Phase(
        "urban", "Urban fares (10:00-12:30)",
        3.0, 5.5, "urban", target_speed=40.0, aggression=0.15,
        trip_break=True,
    ),
    Phase(
        "mamak", "Mamak idle / eat in car (12:30-14:30)",
        5.5, 7.5, "mamak", target_speed=0.0, idle=True, accessory_load=0.8,
        trip_break=True,
    ),
    Phase(
        "mrr2", "MRR2 aggression (14:30-17:00)",
        7.5, 10.0, "mrr2", target_speed=70.0, aggression=0.85,
        harsh_brakes_burst=10, behavior_mix=True, trip_break=True,
        degrade_thermo=0.02,
    ),
    Phase(
        "refuel", "Storm load + Petronas refuel (17:00-18:30)",
        10.0, 11.5, "refuel", target_speed=35.0, accessory_load=1.0,
        refuel=True, trip_break=True, degrade_battery=0.03,
    ),
    Phase(
        "evening", "Evening economy fares (18:30-21:00)",
        11.5, 14.0, "evening", target_speed=45.0, aggression=0.2,
        trip_break=True,
    ),
    Phase(
        "night", "Night haul NSE + service crosses (21:00-01:00)",
        14.0, 18.0, "night", target_speed=90.0, aggression=0.25,
        degrade_battery=0.08, degrade_thermo=0.04,
        force_vehicle_batt=12.05,
        jump_odo=59950.0,
        jump_engine_hours_min=150100.0,
        service_distance=120.0,
        trip_break=True,
    ),
    Phase(
        "fuel_spike", "Fuel spike traffic slog (01:00-03:00)",
        18.0, 20.0, "fuel_spike", target_speed=25.0, aggression=0.4,
        idle=False, fuel_rich=2.8, accessory_load=0.6,
        degrade_thermo=0.12, trip_break=True,
    ),
    Phase(
        "hill", "Genting Sempah cooling failure (03:00-04:30)",
        20.0, 21.5, "hill", target_speed=50.0, aggression=0.3,
        force_coolant=115.0, force_oil=136.0, dtc=("P0217",),
        degrade_thermo=0.5, trip_break=True,
    ),
    Phase(
        "limp", "Limp home short trips (04:30-06:30)",
        21.5, 23.5, "limp", target_speed=30.0, aggression=0.2,
        force_vehicle_batt=11.5, force_tracker_batt=11.4,
        degrade_battery=0.2, trip_break=True,
    ),
    Phase(
        "dead", "Stranded no-start (06:30-07:00)",
        23.5, 24.0, "dead", target_speed=0.0, ignition=False, dead=True,
        force_vehicle_batt=9.5, force_tracker_batt=9.4,
    ),
]


def phase_at(elapsed_h: float, phases: list[Phase] | None = None) -> Phase:
    table = phases or FULL_PHASES
    if elapsed_h < 0:
        return table[0]
    for p in table:
        if p.start_h <= elapsed_h < p.end_h:
            return p
    return table[-1]


def scaled_phases(duration_hours: float) -> list[Phase]:
    """Stretch/compress FULL_PHASES to fit duration_hours (still 1:1 wall clock)."""
    if duration_hours <= 0:
        raise ValueError("duration_hours must be > 0")
    scale = duration_hours / 24.0
    out: list[Phase] = []
    for p in FULL_PHASES:
        out.append(
            Phase(
                key=p.key,
                label=p.label,
                start_h=p.start_h * scale,
                end_h=p.end_h * scale,
                leg=p.leg,
                target_speed=p.target_speed,
                ignition=p.ignition,
                aggression=p.aggression,
                idle=p.idle,
                accessory_load=p.accessory_load,
                refuel=p.refuel,
                force_coolant=p.force_coolant,
                force_oil=p.force_oil,
                force_vehicle_batt=p.force_vehicle_batt,
                force_tracker_batt=p.force_tracker_batt,
                dtc=p.dtc,
                jump_odo=p.jump_odo,
                jump_engine_hours_min=p.jump_engine_hours_min,
                service_distance=p.service_distance,
                degrade_thermo=p.degrade_thermo / max(scale, 1e-6),
                degrade_battery=p.degrade_battery / max(scale, 1e-6),
                fuel_rich=p.fuel_rich,
                trip_break=p.trip_break,
                harsh_brakes_burst=p.harsh_brakes_burst,
                behavior_mix=p.behavior_mix,
                dead=p.dead,
            )
        )
    return out


# Dev timetable: same story beats, real alert holds, ~28 minutes wall.
DEV_PHASES: list[Phase] = [
    Phase("morning", "DEV morning", 0.0, 3 / 60, "morning", target_speed=55.0, aggression=0.1),
    Phase(
        "urban", "DEV urban + trip break", 3 / 60, 6 / 60, "urban",
        target_speed=40.0, trip_break=True,
    ),
    Phase(
        "mamak", "DEV mamak idle 5.5 min", 6 / 60, 12 / 60, "mamak",
        target_speed=0.0, idle=True, accessory_load=0.8, trip_break=True,
    ),
    Phase(
        "mrr2", "DEV MRR2 aggression", 12 / 60, 16 / 60, "mrr2",
        target_speed=70.0, aggression=0.9, harsh_brakes_burst=10,
        behavior_mix=True, trip_break=True,
    ),
    Phase(
        "refuel", "DEV refuel", 16 / 60, 17.5 / 60, "refuel",
        target_speed=30.0, refuel=True, accessory_load=1.0, trip_break=True,
    ),
    Phase(
        "evening", "DEV economy trip", 17.5 / 60, 19.5 / 60, "evening",
        target_speed=50.0, trip_break=True,
    ),
    Phase(
        "night", "DEV night + scheduled jumps", 19.5 / 60, 21 / 60, "night",
        target_speed=90.0, force_vehicle_batt=12.05,
        jump_odo=59950.0, jump_engine_hours_min=150100.0,
        service_distance=120.0, trip_break=True, degrade_battery=0.5,
    ),
    Phase(
        "fuel_spike", "DEV fuel spike", 21 / 60, 22.5 / 60, "fuel_spike",
        target_speed=20.0, fuel_rich=3.0, trip_break=True,
    ),
    Phase(
        "hill", "DEV overheat + oil + DTC", 22.5 / 60, 28.5 / 60, "hill",
        target_speed=40.0, force_coolant=115.0, force_oil=136.0,
        dtc=("P0217",), trip_break=True,
    ),
    Phase(
        "limp", "DEV limp short trips", 28.5 / 60, 31 / 60, "limp",
        target_speed=25.0, force_vehicle_batt=11.5, force_tracker_batt=11.4,
        trip_break=True,
    ),
    Phase(
        "dead", "DEV stranded", 31 / 60, 32 / 60, "dead",
        target_speed=0.0, ignition=False, dead=True,
        force_vehicle_batt=9.5, force_tracker_batt=9.4,
    ),
]

DEV_DURATION_HOURS = 32 / 60  # matches DEV_PHASES end
