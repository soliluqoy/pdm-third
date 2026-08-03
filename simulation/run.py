#!/usr/bin/env python3
"""
PREDICT end-to-end simulator — standalone (this folder only).

Registers one car, opens a Codec 8E TCP session, and drives a continuous
physics-based trip that triggers threshold alerts, DTCs, behavior events,
scheduled service, and predictive (PME) signals.

Requires a running stack (docker compose up) on :8000 + :5123.
Does not import or modify anything under app/.

Usage (from this folder OR repo root):
    python simulation/run.py
    python simulation/run.py --quick          # shorter holds (still real durations for alerts)
    python simulation/run.py --host 127.0.0.1 --interval 2
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# Make local imports work whether launched as `python simulation/run.py`
# or `python run.py` from inside simulation/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from api import PredictApi
from physics import VehiclePhysics
from scenario import DEFAULT_CAR, Scenario
from tcp_client import TeltonikaClient


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PREDICT standalone end-to-end simulator")
    p.add_argument("--api", default="http://localhost:8000", help="PREDICT HTTP base URL")
    p.add_argument("--host", default="127.0.0.1", help="Teltonika TCP host")
    p.add_argument("--port", type=int, default=5123, help="Teltonika TCP port")
    p.add_argument("--interval", type=float, default=2.0, help="Sample interval seconds")
    p.add_argument(
        "--idle-minutes",
        type=float,
        default=None,
        help="Idle hold minutes (default 5; use 1 with --quick)",
    )
    p.add_argument(
        "--quick",
        action="store_true",
        help="Faster scenario: 1 min idle; still respects alert duration_seconds",
    )
    p.add_argument(
        "--smoke",
        action="store_true",
        help="~30s connectivity check only (register + short drive, no alert holds)",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument(
        "--summary-json",
        type=Path,
        default=None,
        help="Write final summary JSON to this path",
    )
    args = p.parse_args(argv)
    _configure_logging(args.verbose)
    log = logging.getLogger("sim")

    idle_minutes = args.idle_minutes
    if idle_minutes is None:
        idle_minutes = 1.0 if args.quick else 5.0

    api = PredictApi(args.api)
    log.info("Checking PREDICT at %s …", args.api)
    try:
        api.health()
    except RuntimeError as e:
        log.error("Stack not reachable: %s", e)
        log.error("Start it with: docker compose up --build")
        return 2

    physics = VehiclePhysics(
        odometer_km=49850.0,
        distance_until_service_km=450.0,
        vin=DEFAULT_CAR["vin"],
    )
    client = TeltonikaClient(args.host, args.port, DEFAULT_CAR["imei"])

    tick_count = {"n": 0}

    def on_tick(msg: str, _phys: VehiclePhysics) -> None:
        tick_count["n"] += 1
        if tick_count["n"] % 15 == 1:
            log.debug("tick %s", msg)

    scenario = Scenario(
        api,
        client,
        physics,
        sample_interval=args.interval,
        idle_minutes=idle_minutes,
        on_tick=on_tick,
    )

    mode = "smoke" if args.smoke else ("quick" if args.quick else "full")
    log.info(
        "Starting %s scenario (idle=%.1f min, interval=%.1fs)",
        mode, idle_minutes, args.interval,
    )
    log.info("Watch the dashboard at %s — car IMEI %s", args.api, DEFAULT_CAR["imei"])
    try:
        summary = scenario.run_smoke() if args.smoke else scenario.run()
    except KeyboardInterrupt:
        log.warning("Interrupted — collecting partial summary")
        summary = scenario.summary()
    finally:
        client.close()

    log.info("")
    log.info("---------- Simulation summary ----------")
    log.info("vehicle_id     : %s", summary.get("vehicle_id"))
    log.info("elapsed        : %.0f s", summary.get("elapsed_s", 0))
    log.info("alerts         : %s total / %s active", summary.get("alerts_total"), summary.get("alerts_active"))
    for title in summary.get("alert_titles") or []:
        log.info("  - %s", title)
    log.info("work orders    : %s", summary.get("workorders"))
    for title in summary.get("workorder_titles") or []:
        log.info("  - %s", title)
    log.info("trips          : %s", summary.get("trips"))
    log.info("driving events : %s  %s", summary.get("driving_events"), summary.get("event_types"))
    prog = summary.get("prognostics")
    if prog:
        log.info(
            "prognostics    : battery=%s brakes=%s oil=%s",
            prog.get("battery_score") or prog.get("battery"),
            prog.get("brake_score") or prog.get("brakes"),
            prog.get("oil_score") or prog.get("oil"),
        )
    log.info("phases         : %s", " -> ".join(summary.get("phases") or []))
    log.info("----------------------------------------")

    if args.summary_json:
        args.summary_json.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        log.info("Wrote %s", args.summary_json)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
