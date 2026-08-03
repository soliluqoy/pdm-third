"""
Post-run verification checklist for the KL Grind FMC150 scenario.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("sim.verify")

REQUIRED_VITALS = (
    "fuel_consumed",
    "fuel_level_liters",
    "engine_hours",
    "vehicle_battery_voltage",
    "engine_oil_pressure",
    "hv_battery_charge",
    "coolant_temperature",
    "odometer",
)


def _vital_types(vitals: Any) -> set[str]:
    out: set[str] = set()
    if vitals is None:
        return out
    if isinstance(vitals, list):
        rows = vitals
    elif isinstance(vitals, dict):
        rows = []
        for g in vitals.get("groups") or []:
            rows.extend(g.get("sensors") or [])
        if not rows:
            rows = vitals.get("sensors") or vitals.get("vitals") or []
        live = vitals.get("live") or {}
        if isinstance(live, dict):
            sensors = live.get("sensors") or {}
            if isinstance(sensors, dict):
                out.update(sensors.keys())
    else:
        rows = []
    for row in rows:
        if isinstance(row, dict):
            st = row.get("sensor_type") or row.get("type")
            if st:
                out.add(str(st))
    return out


def _alert_blob(alerts: list) -> str:
    parts = []
    for a in alerts:
        parts.append(
            " ".join(
                str(a.get(k, ""))
                for k in ("key", "rule_key", "title", "name", "message", "dtc_code")
            ).lower()
        )
    return " | ".join(parts)


def _has_fuel_trip(trips: list) -> bool:
    for t in trips:
        if t.get("is_open"):
            continue
        # Driving API exposes fuel_used (= fuel_end - fuel_start) when cumulative
        # fuel_consumed increased over the trip.
        used = t.get("fuel_used")
        if used is not None:
            try:
                if float(used) > 0:
                    return True
            except (TypeError, ValueError):
                pass
        fs, fe = t.get("fuel_start"), t.get("fuel_end")
        if fs is None or fe is None:
            continue
        try:
            if float(fe) > float(fs):
                return True
        except (TypeError, ValueError):
            continue
    return False


def verify(summary: dict, *, smoke: bool = False) -> tuple[bool, list[str], list[str]]:
    """Return (ok, hard_failures, warnings)."""
    hard: list[str] = []
    warn: list[str] = []

    if summary.get("device_type") != "fmc150":
        hard.append(f"device_type is {summary.get('device_type')!r}, expected 'fmc150'")

    vitals = _vital_types(summary.get("vitals"))
    missing = [s for s in REQUIRED_VITALS if s not in vitals]
    if missing:
        hard.append(f"missing vitals: {', '.join(missing)}")

    if smoke:
        if (summary.get("packets_sent") or 0) < 3:
            hard.append("smoke sent fewer than 3 packets")
        return (len(hard) == 0, hard, warn)

    trips = summary.get("trips") or []
    if not _has_fuel_trip(trips):
        hard.append("no closed trip with fuel_used > 0 (fuel_consumed path)")

    events = summary.get("event_types") or []
    if "idling" not in events:
        hard.append("missing driving event: idling")
    harsh = sum(1 for e in (summary.get("driving_events") or []) if e.get("event_type") == "harsh_brake")
    if harsh < 8 and "harsh_brake" not in events:
        hard.append(f"expected harsh_brake events (>=8), saw {harsh}")

    blob = _alert_blob(summary.get("alerts") or [])
    titles = " ".join(summary.get("alert_titles") or []).lower()
    text = blob + " " + titles

    checks = [
        ("overheat/coolant", ("overheat", "coolant", "engine overheating")),
        ("oil temp", ("oil", "very hot")),
        ("battery low", ("battery", "voltage")),
        ("DTC", ("p0217", "fault code", "dtc")),
        ("scheduled service", ("scheduled", "service interval", "engine hours")),
        ("PME", ("brake", "battery failure", "oil change", "predict")),
    ]
    for label, needles in checks:
        if not any(n in text for n in needles):
            hard.append(f"missing alert coverage: {label}")

    if (summary.get("workorders") or 0) < 1:
        hard.append("expected >=1 work order")

    # Soft
    soft_needles = ("fuel consumption", "anomaly_fuel", "weakening over time", "cooling system drifting")
    if not any(n in text for n in soft_needles):
        warn.append("no anomaly_fuel / baseline anomaly alerts (soft - may need baselines job)")

    ok = len(hard) == 0
    return ok, hard, warn


def report(summary: dict, *, smoke: bool = False) -> int:
    ok, hard, warn = verify(summary, smoke=smoke)
    log.info("")
    log.info("---------- Verification ----------")
    if hard:
        for h in hard:
            log.error("FAIL  %s", h)
    else:
        log.info("PASS  hard checks")
    for w in warn:
        log.warning("WARN  %s", w)
    log.info("---------------------------------")
    return 0 if ok else 1
