#!/usr/bin/env python3
"""
PREDICT — clear historical data without removing cars, rules, or settings.

Clears:
  - sensor chart data (raw readings + continuous aggregates)
  - trips, driving events, driver scores
  - alerts (all statuses), DTC events, health events
  - maintenance log and work orders
  - sensor baselines
  - predictive state (component_health + component_wear_events)
  - optional: vehicle live health / last_seen reset to grey/offline

Keeps:
  - vehicles (cars) — unless --delete-vehicle
  - rules and settings (including scheduled next_due anchors unless --reset-anchors)

Usage (from repo root, db reachable at DATABASE_URL):
    python tools/clear_history.py                  # clear all history (prompts)
    python tools/clear_history.py --yes              # skip confirmation
    python tools/clear_history.py --vehicle-id 3     # one car only
    python tools/clear_history.py --imei 3596…       # resolve car by IMEI
    python tools/clear_history.py --dry-run          # show counts, delete nothing
    python tools/clear_history.py --yes --reset-live # also grey-out last_seen/health
    python tools/clear_history.py --yes --reset-anchors
    python tools/clear_history.py --imei 3596… --delete-vehicle --yes

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

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import async_session_factory, engine
from server.models import (
    Alert,
    ComponentHealth,
    ComponentWearEvent,
    DriverScore,
    DrivingEvent,
    DtcEvent,
    Health,
    HealthEvent,
    MaintenanceLog,
    SensorBaseline,
    SensorReading,
    Setting,
    Trip,
    Vehicle,
    WorkOrder,
)

# Child tables first; alerts ↔ work_orders have circular FKs (both SET NULL).
# Wear events reference trips (SET NULL) — delete wear before trips is fine;
# we still put wear early so a partial failure leaves fewer orphans.
_HISTORY_TABLES = (
    ("maintenance_log", MaintenanceLog),
    ("component_wear_events", ComponentWearEvent),
    ("driving_events", DrivingEvent),
    ("trips", Trip),
    ("driver_scores", DriverScore),
    ("dtc_events", DtcEvent),
    ("health_events", HealthEvent),
    ("sensor_baselines", SensorBaseline),
    ("component_health", ComponentHealth),
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
    if vehicle_id is not None and hasattr(model, "vehicle_id"):
        q = q.where(model.vehicle_id == vehicle_id)
    elif vehicle_id is not None and model is ComponentHealth:
        q = q.where(ComponentHealth.vehicle_id == vehicle_id)
    return (await session.execute(q)).scalar_one()


async def _counts(session: AsyncSession, vehicle_id: int | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for name, model in _HISTORY_TABLES:
        out[name] = await _count(session, model, vehicle_id)
    return out


async def _resolve_vehicle_id(
    session: AsyncSession,
    vehicle_id: int | None,
    imei: str | None,
) -> int | None:
    if vehicle_id is not None and imei is not None:
        raise ValueError("Pass only one of --vehicle-id or --imei")
    if imei is None:
        return vehicle_id
    row = (
        await session.execute(select(Vehicle).where(Vehicle.imei == imei))
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"No car with IMEI {imei}")
    return int(row.id)


async def _reset_live(session: AsyncSession, vehicle_id: int | None) -> int:
    """Set health=GREY and clear last_seen so the fleet shows offline."""
    q = (
        update(Vehicle)
        .values(health=Health.GREY, last_seen=None)
    )
    if vehicle_id is not None:
        q = q.where(Vehicle.id == vehicle_id)
    result = await session.execute(q)
    return result.rowcount or 0


async def _reset_anchors(session: AsyncSession, vehicle_id: int | None) -> int:
    """Remove per-vehicle scheduled next_due keys (rule.{id}.vehicle.{vid}.next_due)."""
    if vehicle_id is not None:
        like = f"rule.%.vehicle.{vehicle_id}.next_due"
        result = await session.execute(
            delete(Setting).where(Setting.key.like(like))
        )
        return result.rowcount or 0

    result = await session.execute(
        delete(Setting).where(Setting.key.like("rule.%.vehicle.%.next_due"))
    )
    return result.rowcount or 0


async def _delete_vehicle(session: AsyncSession, vehicle_id: int) -> None:
    await session.execute(delete(Vehicle).where(Vehicle.id == vehicle_id))


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
    reset_live: bool,
    reset_anchors: bool,
    delete_vehicle: bool,
) -> dict[str, int]:
    before = await _counts(session, vehicle_id)
    if dry_run:
        return before

    for _name, model in _HISTORY_TABLES:
        q = delete(model)
        if vehicle_id is not None:
            if model is ComponentHealth:
                q = q.where(ComponentHealth.vehicle_id == vehicle_id)
            else:
                q = q.where(model.vehicle_id == vehicle_id)
        await session.execute(q)

    extras: dict[str, int] = {}
    if reset_anchors:
        extras["scheduled_anchors"] = await _reset_anchors(session, vehicle_id)
    if reset_live and not delete_vehicle:
        extras["vehicles_reset_live"] = await _reset_live(session, vehicle_id)
    if delete_vehicle:
        if vehicle_id is None:
            raise ValueError("--delete-vehicle requires --vehicle-id or --imei")
        await _delete_vehicle(session, vehicle_id)
        extras["vehicles_deleted"] = 1

    await session.commit()

    if vehicle_id is None:
        await _refresh_aggregates()

    before.update(extras)
    return before


def _print_counts(label: str, counts: dict[str, int]) -> None:
    history_keys = {name for name, _ in _HISTORY_TABLES}
    hist = {k: v for k, v in counts.items() if k in history_keys}
    extra = {k: v for k, v in counts.items() if k not in history_keys}
    total = sum(hist.values())
    print(f"\n{label} ({total:,} history rows):")
    for name, n in hist.items():
        if n:
            print(f"  {name:24s} {n:>10,}")
    for name, n in extra.items():
        if n:
            print(f"  {name:24s} {n:>10,}")


async def _async_main(args: argparse.Namespace) -> int:
    try:
        async with async_session_factory() as session:
            try:
                vehicle_id = await _resolve_vehicle_id(
                    session, args.vehicle_id, args.imei
                )
            except ValueError as e:
                print(f"Error: {e}", file=sys.stderr)
                return 2

            if args.delete_vehicle and vehicle_id is None:
                print("Error: --delete-vehicle requires --vehicle-id or --imei", file=sys.stderr)
                return 2

            scope = f"vehicle_id={vehicle_id}" if vehicle_id else "all vehicles"
            print(f"PREDICT history clear — scope: {scope}")
            if args.reset_live:
                print("  + reset vehicle health/last_seen → grey")
            if args.reset_anchors:
                print("  + clear scheduled next_due anchors")
            if args.delete_vehicle:
                print("  + DELETE vehicle row after history clear")

            counts = await _counts(session, vehicle_id)
            _print_counts("Rows to delete", counts)

            if sum(counts.values()) == 0 and not (
                args.reset_live or args.reset_anchors or args.delete_vehicle
            ):
                print("\nNothing to clear.")
                return 0

            if args.dry_run:
                print("\nDry run — no changes made.")
                return 0

            if not args.yes:
                n = sum(counts.values())
                prompt = f"\nDelete {n:,} history rows"
                if args.delete_vehicle:
                    prompt += f" and vehicle {vehicle_id}"
                prompt += "? [y/N] "
                if input(prompt).strip().lower() not in ("y", "yes"):
                    print("Aborted.")
                    return 1

            deleted = await _clear(
                session,
                vehicle_id,
                dry_run=False,
                reset_live=args.reset_live,
                reset_anchors=args.reset_anchors,
                delete_vehicle=args.delete_vehicle,
            )
            _print_counts("Deleted", deleted)

            if vehicle_id is None and not args.delete_vehicle:
                print("\nContinuous aggregates refreshed (1m / 1h / 1d).")
            elif vehicle_id is not None and not args.delete_vehicle:
                print(
                    "\nNote: fleet-wide continuous aggregates were not refreshed "
                    "(per-car clear only). Chart buckets for other cars are unchanged."
                )

            kept = "Cars, rules, and settings were kept."
            if args.delete_vehicle:
                kept = "Rules and settings were kept; the selected car was deleted."
            if args.reset_anchors:
                kept += " Scheduled next_due anchors were cleared."
            print(f"\nDone. {kept}")
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Clear PREDICT history (trips, charts, alerts, maintenance, PME)."
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--vehicle-id",
        type=int,
        metavar="ID",
        help="Clear history for one car only (keeps the car record unless --delete-vehicle).",
    )
    scope.add_argument(
        "--imei",
        type=str,
        metavar="IMEI",
        help="Same as --vehicle-id, resolved by tracker IMEI.",
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
    parser.add_argument(
        "--reset-live",
        action="store_true",
        help="Reset vehicle health to grey and clear last_seen.",
    )
    parser.add_argument(
        "--reset-anchors",
        action="store_true",
        help="Clear scheduled-rule next_due settings keys for the scope.",
    )
    parser.add_argument(
        "--delete-vehicle",
        action="store_true",
        help="After clearing history, delete the car row (requires --vehicle-id/--imei).",
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
