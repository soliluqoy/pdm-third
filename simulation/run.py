#!/usr/bin/env python3
"""
PREDICT KL Grind simulator — FMC150, real wall-clock 24h taxi shift.

Requires docker compose stack on :8000 + :5123. Stdlib only.

    python simulation/run.py              # REAL 24h + verify
    python simulation/run.py --smoke      # ~30s connectivity
    python simulation/run.py --dev        # ~32 min full feature path
    python simulation/run.py --resume     # continue from checkpoint
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api import PredictApi
from physics import VehiclePhysics
from scenario import DEFAULT_CAR, Scenario, build_phases
from tcp_client import TeltonikaClient
from verify import report


def _configure_logging(verbose: bool, log_file: Path | None) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    stream = logging.StreamHandler(sys.stdout)
    handlers: list[logging.Handler] = [stream]
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="PREDICT KL Grind FMC150 simulator (24h wall clock)")
    p.add_argument("--api", default="http://localhost:8000")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5123)
    p.add_argument("--interval", type=float, default=None, help="Sample interval seconds")
    p.add_argument(
        "--duration-hours",
        type=float,
        default=None,
        help="Override full-run length (default 24; ignored with --dev/--smoke)",
    )
    p.add_argument("--smoke", action="store_true", help="~30s connectivity check")
    p.add_argument("--dev", action="store_true", help="~32 min condensed feature path")
    p.add_argument("--quick", action="store_true", help="Alias for --dev")
    p.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    p.add_argument("--no-verify", action="store_true")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--log-file", type=Path, default=None)
    p.add_argument("--summary-json", type=Path, default=None)
    args = p.parse_args(argv)

    mode = "smoke" if args.smoke else ("dev" if (args.dev or args.quick) else "full")
    if args.interval is None:
        args.interval = 2.0 if mode in ("smoke", "dev") else 5.0

    _configure_logging(args.verbose, args.log_file)
    log = logging.getLogger("sim")

    api = PredictApi(args.api)
    log.info("Checking PREDICT at %s …", args.api)
    try:
        api.health()
    except RuntimeError as e:
        log.error("Stack not reachable: %s", e)
        log.error("Start it with: docker compose up --build")
        return 2

    if mode == "dev":
        phases, duration_hours = build_phases("dev", 0.0)
    elif mode == "smoke":
        phases, duration_hours = build_phases("full", 24.0)
    else:
        hours = 24.0 if args.duration_hours is None else args.duration_hours
        phases, duration_hours = build_phases("full", hours)

    physics = VehiclePhysics(
        odometer_km=49850.0,
        distance_until_service_km=450.0,
        fuel_pct=100.0,
        engine_hours_min=120000.0,
        vin=DEFAULT_CAR["vin"],
    )
    client = TeltonikaClient(args.host, args.port, DEFAULT_CAR["imei"])

    tick_count = {"n": 0}

    def on_tick(msg: str, _phys: VehiclePhysics) -> None:
        tick_count["n"] += 1
        every = 1 if mode == "smoke" else (30 if mode == "dev" else 60)
        if tick_count["n"] % every == 1:
            log.debug("tick %s", msg)

    scenario = Scenario(
        api,
        client,
        physics,
        sample_interval=args.interval,
        duration_hours=duration_hours,
        phases=phases,
        resume=bool(args.resume and mode == "full"),
        on_tick=on_tick,
    )

    log.info(
        "Starting %s scenario (duration=%.2fh, interval=%.1fs)",
        mode, 0.0 if mode == "smoke" else duration_hours, args.interval,
    )
    if mode == "full":
        log.info("REAL wall-clock run (%.1fh) - do not let the host sleep", duration_hours)
    log.info("Dashboard %s - IMEI %s", args.api, DEFAULT_CAR["imei"])

    try:
        if mode == "smoke":
            summary = scenario.run_smoke()
        else:
            summary = scenario.run()
    except KeyboardInterrupt:
        log.warning("Interrupted - collecting partial summary")
        summary = scenario.summary()
        try:
            scenario._save_checkpoint()
        except Exception:
            pass
    finally:
        client.close()

    log.info("")
    log.info("---------- Simulation summary ----------")
    log.info("vehicle_id     : %s", summary.get("vehicle_id"))
    log.info("device_type    : %s", summary.get("device_type"))
    log.info("elapsed        : %.1f h", (summary.get("elapsed_s") or 0) / 3600.0)
    log.info("packets        : %s (reconnects=%s)", summary.get("packets_sent"), summary.get("reconnects"))
    log.info("alerts         : %s total / %s active", summary.get("alerts_total"), summary.get("alerts_active"))
    for title in summary.get("alert_titles") or []:
        log.info("  - %s", title)
    log.info("work orders    : %s", summary.get("workorders"))
    for title in summary.get("workorder_titles") or []:
        log.info("  - %s", title)
    log.info("trips          : %s", summary.get("trips_count"))
    log.info("driving events : %s  %s", summary.get("driving_events_count"), summary.get("event_types"))
    prog = summary.get("prognostics")
    if prog:
        log.info(
            "prognostics    : battery=%s brakes=%s oil=%s",
            prog.get("battery_score"),
            prog.get("brake_score"),
            prog.get("oil_score"),
        )
    log.info("phases         : %s", " -> ".join(summary.get("phases") or []))
    log.info("----------------------------------------")

    if args.summary_json:
        slim = {
            k: v for k, v in summary.items()
            if k not in ("alerts", "trips", "driving_events", "vitals")
        }
        args.summary_json.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")
        log.info("Wrote %s", args.summary_json)

    if args.no_verify:
        return 0
    return report(summary, smoke=(mode == "smoke"))


if __name__ == "__main__":
    raise SystemExit(main())
