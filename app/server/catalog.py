"""
PREDICT — sensor catalog: AVL maps + unified sensor metadata.

One JSON map per device model (server/teltonika/avl/<model>.json) translates
Teltonika AVL IDs → normalized ``sensor_type`` strings. Both models normalize
to the SAME sensor_type for the same physical quantity, so rules and the UI
stay device-agnostic.

``decode_record`` turns one parsed AvlRecord into a NormalizedReading — the
single internal telemetry format the whole pipeline consumes (no MQTT, no
JSON round-trip).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from server.teltonika.codec8e import AvlRecord

logger = logging.getLogger("predict.catalog")

AVL_DIR = Path(__file__).parent / "teltonika" / "avl"

# Teltonika Green driving type (AVL 253) → driving event type
_GREEN_DRIVING_TYPE = {1: "harsh_accel", 2: "harsh_brake", 3: "harsh_corner"}


# ── AVL map loading ───────────────────────────────────────────────────────────
def _load_maps() -> dict[str, dict[int, dict]]:
    maps: dict[str, dict[int, dict]] = {}
    for path in sorted(AVL_DIR.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = raw.get("io_elements", [])
        maps[path.stem.lower()] = {int(e["id"]): e for e in entries}
        logger.info("Loaded %d AVL mappings for %s", len(entries), path.stem)
    if not maps:
        raise RuntimeError(f"No AVL map files found in {AVL_DIR}")
    return maps


IO_MAPS: dict[str, dict[int, dict]] = _load_maps()
DEVICE_MODELS = tuple(sorted(IO_MAPS))           # ("fmc001", "fmc150")
DEFAULT_MODEL = "fmc001"


# ── Unified sensor metadata (display grouping + formatting) ──────────────────
# group: which section of the car page the vital belongs to.
SENSOR_META: dict[str, dict] = {
    # Engine
    "engine_rpm":             {"name": "Engine RPM",            "unit": "RPM",  "group": "engine",  "decimals": 0},
    "coolant_temperature":    {"name": "Coolant",               "unit": "°C",   "group": "engine",  "decimals": 0},
    "engine_oil_temperature": {"name": "Oil Temperature",       "unit": "°C",   "group": "engine",  "decimals": 0},
    "engine_oil_pressure":    {"name": "Oil Pressure",          "unit": "kPa",  "group": "engine",  "decimals": 0},
    "engine_oil_level":       {"name": "Oil Level",             "unit": "%",    "group": "engine",  "decimals": 0},
    "engine_load":            {"name": "Engine Load",           "unit": "%",    "group": "engine",  "decimals": 0},
    "intake_map":             {"name": "Intake Pressure",       "unit": "kPa",  "group": "engine",  "decimals": 0},
    "intake_air_temperature": {"name": "Intake Air",            "unit": "°C",   "group": "engine",  "decimals": 0},
    "throttle_position":      {"name": "Throttle",              "unit": "%",    "group": "engine",  "decimals": 0},
    "engine_runtime":         {"name": "Runtime This Trip",     "unit": "s",    "group": "engine",  "decimals": 0},
    "engine_hours":           {"name": "Engine Hours",          "unit": "min",  "group": "engine",  "decimals": 0},
    # Battery & electrical
    "battery_voltage":          {"name": "Battery (tracker feed)", "unit": "V", "group": "battery", "decimals": 1},
    "vehicle_battery_voltage":  {"name": "Battery (car)",          "unit": "V", "group": "battery", "decimals": 1},
    "control_module_voltage":   {"name": "ECU Voltage",            "unit": "V", "group": "battery", "decimals": 1},
    "tracker_battery_voltage":  {"name": "Tracker Battery",        "unit": "V", "group": "battery", "decimals": 1},
    "hv_battery_charge":        {"name": "HV Battery Charge",      "unit": "%", "group": "battery", "decimals": 0},
    # Fuel
    "fuel_level":         {"name": "Fuel Level",        "unit": "%",   "group": "fuel", "decimals": 0},
    "fuel_level_liters":  {"name": "Fuel (liters)",     "unit": "L",   "group": "fuel", "decimals": 1},
    "fuel_consumed":      {"name": "Fuel Used (total)", "unit": "L",   "group": "fuel", "decimals": 1},
    "fuel_rate":          {"name": "Fuel Rate",         "unit": "L/h", "group": "fuel", "decimals": 1},
    "remaining_distance": {"name": "Range Remaining",   "unit": "km",  "group": "fuel", "decimals": 0},
    # Vehicle / trip
    "vehicle_speed":         {"name": "Speed (GPS)",       "unit": "km/h", "group": "vehicle", "decimals": 0},
    "vehicle_speed_obd":     {"name": "Speed (ECU)",       "unit": "km/h", "group": "vehicle", "decimals": 0},
    "odometer":              {"name": "Odometer",          "unit": "km",   "group": "vehicle", "decimals": 0},
    "distance_until_service": {"name": "Service Due In",   "unit": "km",   "group": "vehicle", "decimals": 0},
    "ambient_air_temperature": {"name": "Outside Temp",    "unit": "°C",   "group": "vehicle", "decimals": 0},
    "mil_on_distance":       {"name": "Distance w/ Check-Engine", "unit": "km", "group": "vehicle", "decimals": 0},
    # Tracker
    "gsm_signal":    {"name": "GSM Signal",       "unit": "/5", "group": "tracker", "decimals": 0},
    "dtc_count":     {"name": "Fault Code Count", "unit": "",   "group": "tracker", "decimals": 0},
    "codes_cleared_distance": {"name": "Distance Since Codes Cleared", "unit": "km", "group": "tracker", "decimals": 0},
}

GROUP_LABELS = {
    "engine": "Engine",
    "battery": "Battery & Electrical",
    "fuel": "Fuel",
    "vehicle": "Vehicle",
    "tracker": "Tracker",
    "other": "Other",
}

# Which sensor_types are worth a chart / vitals tile (everything is stored,
# but the UI leads with these).
PRIMARY_SENSORS = (
    "vehicle_speed_obd", "engine_rpm", "coolant_temperature", "fuel_level",
    "battery_voltage", "vehicle_battery_voltage", "control_module_voltage",
    "odometer", "engine_oil_temperature", "distance_until_service",
)


def sensor_meta(sensor_type: str) -> dict:
    meta = SENSOR_META.get(sensor_type)
    if meta:
        return meta
    return {
        "name": sensor_type.replace("_", " ").title(),
        "unit": "",
        "group": "other",
        "decimals": 1,
    }


def sensors_for_model(model: str) -> list[dict]:
    """Catalog of what a device model can report (for Settings / setup UI)."""
    out = []
    for entry in IO_MAPS.get(model, {}).values():
        if entry.get("kind", "sensor") != "sensor":
            continue
        st = entry["sensor_type"]
        meta = sensor_meta(st)
        out.append({
            "sensor_type": st,
            "name": meta["name"],
            "unit": entry.get("unit") or meta["unit"],
            "group": meta["group"],
            "avl_id": entry["id"],
        })
    # de-dupe by sensor_type (same type can appear once per model)
    seen: dict[str, dict] = {}
    for row in out:
        seen.setdefault(row["sensor_type"], row)
    return sorted(seen.values(), key=lambda r: (r["group"], r["name"]))


# ── Normalized reading (internal telemetry format) ────────────────────────────
@dataclass
class NormalizedReading:
    timestamp: datetime
    sensors: dict[str, float] = field(default_factory=dict)   # sensor_type → value
    units: dict[str, str] = field(default_factory=dict)       # sensor_type → unit
    ignition: Optional[bool] = None
    movement: Optional[bool] = None
    vin: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed: Optional[float] = None        # GNSS speed, km/h
    satellites: Optional[int] = None
    dtcs: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)  # device eco-driving events


_unmapped_seen: set[tuple[str, int]] = set()


def decode_record(record: AvlRecord, model: str) -> NormalizedReading:
    """AvlRecord + model AVL map → NormalizedReading.

    Unmapped AVL IDs are logged once per process — that is the discovery tool
    when a car exposes parameters not yet in the map.
    """
    io_map = IO_MAPS.get(model) or IO_MAPS[DEFAULT_MODEL]
    out = NormalizedReading(
        timestamp=record.timestamp,
        latitude=record.latitude,
        longitude=record.longitude,
        speed=float(record.speed),
        satellites=record.satellites,
    )

    green_type: Optional[int] = None
    green_value: Optional[float] = None
    overspeed_value: Optional[float] = None

    for avl_id, raw in record.io.items():
        entry = io_map.get(avl_id)
        if entry is None:
            key = (model, avl_id)
            if key not in _unmapped_seen:
                _unmapped_seen.add(key)
                logger.info(
                    "Unmapped AVL ID %s (value=%r) on %s — add it to avl/%s.json to use it",
                    avl_id, raw, model, model,
                )
            continue

        if isinstance(raw, (bytes, bytearray)):
            if entry.get("encoding") == "ascii":
                value = raw.decode("ascii", errors="ignore").strip("\x00").strip()
            else:
                value = raw.hex()
        else:
            value = raw * entry.get("scale", 1) + entry.get("offset", 0)
            if isinstance(value, float):
                value = round(value, 4)

        kind = entry.get("kind", "sensor")
        sensor_type = entry["sensor_type"]

        if kind == "sensor":
            try:
                out.sensors[sensor_type] = float(value)
                out.units[sensor_type] = entry.get("unit", "")
            except (TypeError, ValueError):
                pass
        elif kind == "meta":
            if sensor_type == "ignition":
                out.ignition = bool(int(value)) if not isinstance(value, str) else bool(value)
            elif sensor_type == "movement":
                out.movement = bool(int(value)) if not isinstance(value, str) else bool(value)
            elif sensor_type == "vin":
                out.vin = str(value)
        elif kind == "dtc":
            # Comma-separated codes in one ASCII element ("P0128,P0300").
            for code in str(value).split(","):
                code = code.strip()
                if code:
                    out.dtcs.append(code)
        elif kind == "event":
            try:
                if sensor_type == "green_driving_type":
                    green_type = int(value)
                elif sensor_type == "green_driving_value":
                    green_value = float(value)
                elif sensor_type == "overspeeding":
                    overspeed_value = float(value)
            except (TypeError, ValueError):
                pass

    if green_type in _GREEN_DRIVING_TYPE:
        out.events.append({
            "event_type": _GREEN_DRIVING_TYPE[green_type],
            "value": green_value if green_value is not None else float(green_type),
        })
    if overspeed_value is not None and overspeed_value > 0:
        out.events.append({"event_type": "speeding", "value": overspeed_value})

    return out
