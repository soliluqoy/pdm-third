#!/usr/bin/env python3
"""
PREDICT — seed synthetic historical data so the /predictive page works without
a full fleet or real telemetry.

The ML layer (services/models.py) consumes ONLY per-vehicle daily feature rows
(vehicle_features). In production those are built from raw sensor readings by
services/features.py; this script writes realistic feature rows directly, then
trains the models through the exact production path (services.models._train_component)
so the UI behaves identically.

What it does:
  1. Registers N synthetic vehicles (unique IMEIs) — idempotent.
  2. Seeds ~DAYS_PER_VEHICLE per-vehicle daily VehicleFeature rows.
     - HEALTHY vehicles: normal ranges with small daily noise.
     - FAILING vehicles: deliberately degrading values so Isolation Forest
       scores them > 80 (anomaly) — the fleet row spikes red in the UI.
  3. Optionally seeds FailureEvents (failed vehicle + component) so the
     Model evaluation card shows real precision/recall.
  4. Trains all 4 component models (battery / cooling / oil / engine) via the
     production training functions → models/isolation_*.joblib.
  5. Refreshes the in-process vehicle registry so live scoring/alerts pick up
     the new cars without restarting.

Usage:
    docker compose exec app python tools/seed_predictive_demo.py
    docker compose exec app python tools/seed_predictive_demo.py --no-failures
    docker compose exec app python tools/seed_predictive_demo.py --days 30
    python tools/seed_predictive_demo.py        (local, PYTHONPATH=app)

Notes:
- Idempotent: rerunning updates existing rows / skips training if a model file
  already exists (pass --force-train to retrain).
- Deletes NO data. To start over: python tools/clear_history.py --yes
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Allow `from server.*` when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.db import async_session_factory, engine
from server.models import (
    FailureEvent,
    Health,
    Vehicle,
    VehicleFeature,
)

# ──────────────────────────────────────────────────────────────────────────────
# Fleet configuration
# ──────────────────────────────────────────────────────────────────────────────
# (name, imei, license_plate, make, model, kind)  kind = healthy | battery | cooling
FLEET = [
    ("Taxi KL-01 Proton Saga",   "524000000000001", "WDL 1234 A", "Proton", "Saga FLX",    "healthy"),
    ("Taxi KL-02 Toyota Vios",   "524000000000002", "WDL 5678 B", "Toyota", "Vios",        "healthy"),
    ("Taxi KL-03 Honda City",    "524000000000003", "WDL 9012 C", "Honda",  "City",        "healthy"),
    ("Taxi KL-04 Perodua Bezza", "524000000000004", "WDL 3456 D", "Perodua","Bezza",       "healthy"),
    ("Taxi KL-07 Perodua Myvi",  "524000000000007", "WDL 6789 G", "Perodua","Myvi",        "healthy"),
    ("Taxi KL-08 Toyota Camry",  "524000000000008", "WDL 1122 H", "Toyota", "Camry",       "healthy"),
    ("Taxi KL-09 Honda Jazz",    "524000000000009", "WDL 3344 I", "Honda",  "Jazz",        "healthy"),
    ("Taxi KL-10 Proton Iriz",   "524000000000010", "WDL 5566 J", "Proton", "Iriz",        "healthy"),
    ("Taxi KL-11 Nissan Sylphy", "524000000000011", "WDL 7788 K", "Nissan", "Sylphy",      "healthy"),
    ("Taxi KL-12 Hyundai Elantra","524000000000012","WDL 9900 L", "Hyundai","Elantra",     "healthy"),
    ("Taxi KL-05 Nissan Almera", "524000000000005", "WDL 7890 E", "Nissan", "Almera",      "battery"),
    ("Taxi KL-06 Hyundai Accent","524000000000006", "WDL 2345 F", "Hyundai","Accent",      "cooling"),
]

# How many daily feature rows per vehicle.
DAYS_PER_VEHICLE = 20
# Base "today" for the feature rows (UTC date).
START_DATE = date.today() - timedelta(days=DAYS_PER_VEHICLE)

# Failure seeds for evaluation: (fleet index, component, days_before_failure)
# Indices 10 and 11 are the two failing vehicles (after the 10 healthy ones).
# Failures happen near the END of the window so the 14-day evaluation lookback
# includes the degrading days (degradation starts at DAYS_PER_VEHICLE - 5).
FAILURE_SEEDS = [
    (10, "battery", 3),    # battery car failed 3 days ago (degrading days 15-17 in lookback)
    (11, "cooling", 1),    # cooling car failed 1 day ago (degrading days 15-18 in lookback)
]


# ── Deterministic-ish RNG so reruns look the same ────────────────────────────
def _rng(*, seed: int) -> random.Random:
    return random.Random(seed)


# ──────────────────────────────────────────────────────────────────────────────
# Feature-row generation
# ──────────────────────────────────────────────────────────────────────────────
def _bounded(rng: random.Random, base: float, spread: float, *, lo: float, hi: float) -> float:
    return max(lo, min(hi, base + rng.uniform(-spread, spread)))


def _healthy_row(day_offset: int, rng: random.Random) -> dict:
    """A normal taxi day — small daily noise around realistic fleet values."""
    day = START_DATE + timedelta(days=day_offset)
    odo = 45_000 + 120 * day_offset + rng.uniform(0, 25)
    return {
        "date": day,
        # usage
        "odometer": round(odo, 1),
        "engine_hours": round(3_200 + 6.0 * day_offset + rng.uniform(0, 1.5), 1),
        "distance_km": round(rng.uniform(90, 210), 1),
        "trip_count": rng.randint(6, 14),
        "duration_seconds": rng.randint(7_200, 16_000),
        # battery
        "battery_mean_v": round(_bounded(rng, 14.15, 0.10, lo=13.9, hi=14.4), 3),
        "battery_min_v": round(_bounded(rng, 13.0, 0.12, lo=12.7, hi=13.3), 3),
        "battery_std_v": round(rng.uniform(0.05, 0.12), 4),
        "battery_trend_v_per_day": round(rng.uniform(-0.002, 0.002), 5),
        # cooling
        "coolant_mean_c": round(_bounded(rng, 88.0, 1.5, lo=85.0, hi=91.0), 2),
        "coolant_p95_c": round(_bounded(rng, 94.0, 1.2, lo=91.5, hi=96.5), 2),
        "coolant_max_c": round(_bounded(rng, 99.0, 1.5, lo=96.0, hi=102.0), 2),
        "coolant_trend_c_per_day": round(rng.uniform(-0.02, 0.02), 4),
        # oil
        "oil_temp_mean_c": round(_bounded(rng, 98.0, 1.5, lo=95.0, hi=101.0), 2),
        "oil_temp_max_c": round(_bounded(rng, 106.0, 1.5, lo=103.0, hi=109.0), 2),
        "thermal_minutes": round(rng.uniform(0.0, 0.4), 2),
        # engine
        "rpm_mean": round(_bounded(rng, 2_100, 120, lo=1_900, hi=2_300), 1),
        "rpm_max": round(_bounded(rng, 4_300, 250, lo=3_900, hi=4_700), 1),
        "high_rpm_minutes": round(rng.uniform(1.0, 4.0), 2),
        # behavior
        "harsh_events": rng.randint(0, 2),
        "idle_ratio": round(rng.uniform(0.12, 0.20), 3),
        # z-scores (drift from baseline) — healthy = near zero
        "battery_z": round(rng.uniform(-0.2, 0.2), 3),
        "coolant_z": round(rng.uniform(-0.15, 0.15), 3),
        "oil_temp_z": round(rng.uniform(-0.2, 0.2), 3),
    }


def _battery_fail_row(day_offset: int, rng: random.Random) -> dict:
    """Battery healthy for most days, then degrades sharply in the last ~5 days.
    Keeping the extreme rows a small minority lets Isolation Forest isolate them
    (contamination=0.05) and the latest row scores > 80."""
    row = _healthy_row(day_offset, rng)
    # Only degrade in the final DEGRADE_DAYS of the window.
    degrade_start = DAYS_PER_VEHICLE - 5
    if day_offset < degrade_start:
        return row
    frac = (day_offset - degrade_start) / max(DAYS_PER_VEHICLE - 1 - degrade_start, 1)
    row["battery_mean_v"] = round(_bounded(rng, 13.6 - 2.2 * frac, 0.08, lo=10.8, hi=13.9), 3)
    row["battery_min_v"] = round(_bounded(rng, 12.2 - 2.2 * frac, 0.12, lo=9.4, hi=12.8), 3)
    row["battery_std_v"] = round(rng.uniform(0.18, 0.32), 4)
    row["battery_trend_v_per_day"] = round(-0.15 - 0.40 * frac, 4)
    row["battery_z"] = round(-1.8 - 3.6 * frac, 3)
    return row


def _cooling_fail_row(day_offset: int, rng: random.Random) -> dict:
    """Cooling/oil healthy for most days, then degrades sharply in the last ~5
    days so the extreme rows stay a small minority and score > 80."""
    row = _healthy_row(day_offset, rng)
    degrade_start = DAYS_PER_VEHICLE - 5
    if day_offset < degrade_start:
        return row
    frac = (day_offset - degrade_start) / max(DAYS_PER_VEHICLE - 1 - degrade_start, 1)
    row["coolant_mean_c"] = round(_bounded(rng, 92.0 + 34.0 * frac, 1.2, lo=86.0, hi=126.0), 2)
    row["coolant_p95_c"] = round(_bounded(rng, 99.0 + 36.0 * frac, 1.0, lo=92.0, hi=134.0), 2)
    row["coolant_max_c"] = round(_bounded(rng, 104.0 + 38.0 * frac, 1.0, lo=97.0, hi=140.0), 2)
    row["coolant_trend_c_per_day"] = round(0.20 + 0.90 * frac, 4)
    row["coolant_z"] = round(1.6 + 4.0 * frac, 3)
    row["oil_temp_mean_c"] = round(_bounded(rng, 103.0 + 28.0 * frac, 1.2, lo=96.0, hi=130.0), 2)
    row["oil_temp_max_c"] = round(_bounded(rng, 111.0 + 32.0 * frac, 1.0, lo=104.0, hi=142.0), 2)
    row["thermal_minutes"] = round(2.0 + 36.0 * frac + rng.uniform(0, 2), 2)
    row["oil_temp_z"] = round(1.4 + 3.8 * frac, 3)
    return row


ROW_BUILDERS = {
    "healthy": _healthy_row,
    "battery": _battery_fail_row,
    "cooling": _cooling_fail_row,
}


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────
async def _get_vehicle_by_imei(session: AsyncSession, imei: str) -> Vehicle | None:
    return (
        await session.execute(select(Vehicle).where(Vehicle.imei == imei))
    ).scalar_one_or_none()


async def _upsert_vehicle(session: AsyncSession, spec: tuple) -> Vehicle:
    """spec is a FLEET tuple: (name, imei, license_plate, make, model, kind)."""
    name, imei, plate, make, model, _kind = spec
    existing = await _get_vehicle_by_imei(session, imei)
    if existing:
        # Keep the existing row; update the mutable fields for consistency.
        for field, value in (
            ("name", name),
            ("license_plate", plate),
            ("make", make),
            ("model", model),
        ):
            setattr(existing, field, value)
        return existing
    v = Vehicle(
        name=name,
        imei=imei,
        license_plate=plate,
        make=make,
        model=model,
        year=2018,
        device_type="fmc150",
        mass_kg=1450.0,
        oil_capacity_l=5.0,
        brake_pad_capacity_mj=800.0,
        health=Health.GREY,
    )
    session.add(v)
    await session.flush()
    return v


async def _seed_vehicle_rows(
    session: AsyncSession,
    vehicle: Vehicle,
    kind: str,
    days: int,
) -> int:
    """Upsert DAYS_PER_VEHICLE daily feature rows for one vehicle."""
    builder = ROW_BUILDERS[kind]
    rng = _rng(seed=vehicle.imei[-4:])
    written = 0
    for offset in range(days):
        spec = builder(offset, rng)
        existing = (
            await session.execute(
                select(VehicleFeature).where(
                    VehicleFeature.vehicle_id == vehicle.id,
                    VehicleFeature.date == spec["date"],
                )
            )
        ).scalar_one_or_none()
        if existing:
            for k, v in spec.items():
                setattr(existing, k, v)
            continue
        session.add(VehicleFeature(vehicle_id=vehicle.id, **spec))
        written += 1
    return written


async def _seed_failure_events(session: AsyncSession, vehicles: dict[int, Vehicle]) -> int:
    """Insert FailureEvents for the flagged cars (idempotent by vehicle+component)."""
    added = 0
    for fleet_idx, component, days_before in FAILURE_SEEDS:
        if fleet_idx >= len(FLEET):
            continue
        vehicle = vehicles[fleet_idx]
        exists = (
            await session.execute(
                select(FailureEvent).where(
                    FailureEvent.vehicle_id == vehicle.id,
                    FailureEvent.component == component,
                )
            )
        ).scalar_one_or_none()
        if exists:
            continue
        occurred_day = START_DATE + timedelta(days=DAYS_PER_VEHICLE - days_before)
        # FailureEvent.occurred_at is TIMESTAMPTZ — pass a real tz-aware datetime.
        occurred = datetime(occurred_day.year, occurred_day.month, occurred_day.day,
                            tzinfo=timezone.utc)
        session.add(FailureEvent(
            vehicle_id=vehicle.id,
            component=component,
            symptom=f"Synthetic {component} failure for the predictive demo",
            odometer=45_000 + 120 * (DAYS_PER_VEHICLE - days_before),
            occurred_at=occurred,
        ))
        added += 1
    return added


async def _print_summary(session: AsyncSession) -> None:
    """Print a compact report so the user sees the result without the browser."""
    from server.services import models as models_service

    print("\n" + "=" * 68)
    print("PREDICT predictive demo — summary")
    print("=" * 68)

    # 1. Model status (matches GET /api/v1/models/status)
    print("\nModel status:")
    for component, columns in models_service.COMPONENT_FEATURES.items():
        loaded = models_service._load_model(component)
        if loaded is None:
            print(f"  {component:9s} not_trained")
        else:
            print(
                f"  {component:9s} trained  v{loaded['version']}  "
                f"at {loaded['trained_at']}"
            )

    # 2. Per-vehicle scores (matches scoring path)
    print("\nFleet anomaly scores (latest feature row):")
    vehicles = list((await session.execute(
        select(Vehicle).where(Vehicle.imei.in_([s[1] for s in FLEET]))
    )).scalars().all())
    for v in sorted(vehicles, key=lambda x: x.name):
        scores = await models_service.score_vehicle(session, v.id)
        if not scores:
            print(f"  {v.name:28s} (no score — no trained model)")
            continue
        parts = "  ".join(
            f"{c}={score:5.1f}{' ⚠' if score >= 80 else ''}"
            for c, score in scores.items()
        )
        print(f"  {v.name:28s} {parts}")

    # 3. Evaluation (if failures were seeded)
    ev = await models_service.evaluate(session)
    if ev.get("status") == "ok":
        print("\nEvaluation (precision / recall):")
        for component, r in ev["results"].items():
            if r.get("status") == "evaluated":
                print(
                    f"  {component:9s} P={r['precision']:.0%}  R={r['recall']:.0%}  "
                    f"(tp={r['true_positives']} fp={r['false_positives']} fn={r['false_negatives']})"
                )
            else:
                print(f"  {component:9s} not_trained")
    else:
        print(f"\nEvaluation: {ev.get('detail', ev.get('status'))}")

    # 4. Failure events
    failures = (await session.execute(
        select(FailureEvent).order_by(FailureEvent.id)
    )).scalars().all()
    if failures:
        print("\nFailure events:")
        for f in failures:
            v = await session.get(Vehicle, f.vehicle_id)
            print(f"  vehicle {v.name if v else f.vehicle_id} · {f.component} · {f.occurred_at}")
    print("\n" + "=" * 68)


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
async def _async_main(args: argparse.Namespace) -> int:
    from server.ingest import registry
    from server.services import models as models_service

    async with async_session_factory() as session:
        # 1. Upsert vehicles
        print("Seeding synthetic vehicles…")
        vehicles: dict[int, Vehicle] = {}
        for idx, spec in enumerate(FLEET):
            v = await _upsert_vehicle(session, spec)
            vehicles[idx] = v
            registry.register(v)  # keep in-memory registry fresh
        await session.commit()

        # 2. Seed feature rows
        print(f"Seeding {DAYS_PER_VEHICLE} daily feature rows per vehicle…")
        total_rows = 0
        for idx, v in vehicles.items():
            kind = FLEET[idx][5]
            n = await _seed_vehicle_rows(session, v, kind, DAYS_PER_VEHICLE)
            total_rows += n
        await session.commit()

        # 3. (Optional) failure labels for evaluation
        if args.failures:
            n = await _seed_failure_events(session, vehicles)
            print(f"Seeded {n} synthetic failure event(s) for evaluation.")
            await session.commit()

        # 4. Train models (production path) — only if missing unless forced
        print("Training component models…")
        for component in models_service.COMPONENT_FEATURES:
            if args.force_train or models_service._load_model(component) is None:
                ok = await models_service._train_component(session, component)
                if ok:
                    print(f"  {component}: trained on fleet features")
                else:
                    print(
                        f"  {component}: skipped (< {models_service.MIN_TRAIN_ROWS} valid rows)"
                    )
                await session.commit()
            else:
                print(f"  {component}: model already exists (use --force-train to retrain)")

        # 5. Print the summary
        await _print_summary(session)

    await engine.dispose()
    return 0


def main() -> int:
    global DAYS_PER_VEHICLE, START_DATE
    parser = argparse.ArgumentParser(
        description="Seed synthetic data + train models so /predictive works end-to-end."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DAYS_PER_VEHICLE,
        help=f"Daily feature rows per vehicle (default {DAYS_PER_VEHICLE}).",
    )
    parser.add_argument(
        "--no-failures",
        dest="failures",
        action="store_false",
        help="Do NOT seed synthetic FailureEvents (evaluation stays 'waiting').",
    )
    parser.set_defaults(failures=True)
    parser.add_argument(
        "--force-train",
        action="store_true",
        help="Retrain all component models even if a model file already exists.",
    )
    args = parser.parse_args()

    if args.days != DAYS_PER_VEHICLE:
        DAYS_PER_VEHICLE = args.days
        START_DATE = date.today() - timedelta(days=DAYS_PER_VEHICLE)

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