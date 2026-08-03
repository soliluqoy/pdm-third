#!/usr/bin/env python3
"""
PREDICT — clear historical data without removing cars, rules, or settings.

Clears:
  - sensor chart data (raw readings + continuous aggregates)
  - trips, driving events, driver scores
  - alerts (all statuses), DTC events, health events
  - maintenance log and work orders
  - sensor baselines

Keeps:
  - vehicles (cars)
  - rules and settings

Usage (from repo root, db reachable at DATABASE_URL):
    python tools/clear_history.py                  # clear all history (prompts)
    python tools/clear_history.py --yes              # skip confirmation
    python tools/clear_history.py --vehicle-id 3     # one car only
    python tools/clear_history.py --dry-run          # show counts, delete nothing

Docker (when local Python deps differ from the app image):
    docker compose run --rm -v ./tools:/tools app python /tools/clear_history.py --yes

Requires app dependencies (asyncpg, sqlalchemy, pydantic-settings). From repo root:
    pip install -r app/requirements.txt
    set PYTHONPATH=app          (Windows)
    export PYTHONPATH=app       (Linux/macOS)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Allow `from server.*` when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import async_session_factory, engine
from server.models import (
    Alert,
    DriverScore,
    DrivingEvent,
    DtcEvent,
    HealthEvent,
    MaintenanceLog,
    SensorBaseline,
    SensorReading,
    Trip,
    WorkOrder,
)

# Child tables first; alerts ↔ work_orders have circular FKs (both SET NULL).
_HISTORY_TABLES = (
    ("maintenance_log", MaintenanceLog),
    ("driving_events", DrivingEvent),
    ("trips", Trip),
    ("driver_scores", DriverScore),
    ("dtc_events", DtcEvent),
    ("health_events", HealthEvent),
    ("sensor_baselines", SensorBaseline),
    ("work_orders", WorkOrder),
    ("alerts", Alert),
    ("sensor_readings", SensorReading),
)

_CONTINUOUS_AGGREGATES = (
    "sensor_readings_1m",
    "sensor_readings_1h",
    "sensor_readings_1d",
)


async def _count(session: AsyncSession, model, vehicle_id: int | None) -> int:
    q = select(func.count()).select_from(model)
    if vehicle_id is not None:
        q = q.where(model.vehicle_id == vehicle_id)
    return (await session.execute(q)).scalar_one()


async def _counts(session: AsyncSession, vehicle_id: int | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, model in _HISTORY_TABLES:
        out[name] = await _count(session, model, vehicle_id)
    return out


async def _refresh_aggregates() -> None:
    """TimescaleDB requires refresh_continuous_aggregate outside a transaction."""
    async with engine.connect() as conn:
        autocommit = await conn.execution_options(isolation_level="AUTOCOMMIT")
        for view in _CONTINUOUS_AGGREGATES:
            await autocommit.execute(
                text(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
            )


async def _clear(
    session: AsyncSession,
    vehicle_id: int | None,
    *,
    dry_run: bool,
) -> dict[str, int]:
    before = await _counts(session, vehicle_id)
    if dry_run:
        return before

    for _name, model in _HISTORY_TABLES:
        q = delete(model)
        if vehicle_id is not None:
            q = q.where(model.vehicle_id == vehicle_id)
        await session.execute(q)

    await session.commit()

    if vehicle_id is None:
        await _refresh_aggregates()

    return before


def _print_counts(label: str, counts: dict[str, int]) -> None:
    total = sum(counts.values())
    print(f"\n{label} ({total:,} rows total):")
    for name, n in counts.items():
        if n:
            print(f"  {name:20s} {n:>10,}")


async def _async_main(args: argparse.Namespace) -> int:
    try:
        scope = f"vehicle_id={args.vehicle_id}" if args.vehicle_id else "all vehicles"
        print(f"PREDICT history clear — scope: {scope}")

        async with async_session_factory() as session:
            counts = await _counts(session, args.vehicle_id)
            _print_counts("Rows to delete", counts)

            if sum(counts.values()) == 0:
                print("\nNothing to clear.")
                return 0

            if args.dry_run:
                print("\nDry run — no changes made.")
                return 0

            if not args.yes:
                prompt = f"\nDelete {sum(counts.values()):,} history rows? [y/N] "
                if input(prompt).strip().lower() not in ("y", "yes"):
                    print("Aborted.")
                    return 1

            deleted = await _clear(session, args.vehicle_id, dry_run=False)
            _print_counts("Deleted", deleted)

            if args.vehicle_id is None:
                print("\nContinuous aggregates refreshed (1m / 1h / 1d).")
            else:
                print(
                    "\nNote: fleet-wide continuous aggregates were not refreshed "
                    "(per-car clear only). Chart buckets for other cars are unchanged."
                )

            print("\nDone. Cars, rules, and settings were kept.")
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear PREDICT history (trips, charts, alerts, maintenance)."
    )
    parser.add_argument(
        "--vehicle-id",
        type=int,
        metavar="ID",
        help="Clear history for one car only (keeps the car record).",
    )
    parser.add_argument(
        "--yes", "-y",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show row counts without deleting.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_async_main(args))
    except KeyboardInterrupt:
        print("\nAborted.")
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
