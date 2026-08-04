"""
PREDICT — unsupervised failure-prediction models (Isolation Forest).

Trains one Isolation Forest per component (battery / cooling / oil / engine)
on the per-vehicle daily feature vectors (VehicleFeature). The model flags
vehicles whose behavior is drifting from the fleet norm — a data-driven
anomaly score that feeds the normal Alert → suggested WorkOrder loop.

Statistical anomaly layer (not physical health). PME health scores live in
predictor.py and use the opposite polarity (higher = healthier).

Design:
  - Models are trained on the fleet's feature rows (no labels needed).
  - Trained models are persisted to disk (joblib) and versioned.
  - Each vehicle's latest feature row is scored per component → 0-100
    anomaly score (higher = more anomalous).
  - Scores above ml.anomaly_threshold_* fire an anomaly rule (shadow mode).
  - FailureEvent completions label VehicleFeature.failed for evaluate().

Runs as a background job (see start()/run_once()).
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server import settings_store
from server.config import settings
from server.db import async_session_factory
from server.models import (
    FailureEvent,
    Rule,
    RuleType,
    Severity,
    Vehicle,
    VehicleFeature,
    WorkOrderPriority,
)
from server.rules import fire_rule, invalidate_rules_cache

logger = logging.getLogger("predict.models")

# ── Model directory (persisted across restarts) ──────────────────────────────
MODEL_DIR = Path(os.environ.get("PREDICT_MODEL_DIR", "models"))
MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Which feature columns feed each component model.
COMPONENT_FEATURES: dict[str, list[str]] = {
    "battery": [
        "battery_mean_v", "battery_min_v", "battery_std_v",
        "battery_trend_v_per_day", "battery_z",
    ],
    "cooling": [
        "coolant_mean_c", "coolant_p95_c", "coolant_max_c",
        "coolant_trend_c_per_day", "coolant_z",
    ],
    "oil": [
        "oil_temp_mean_c", "oil_temp_max_c", "thermal_minutes", "oil_temp_z",
    ],
    "engine": [
        "rpm_mean", "rpm_max", "high_rpm_minutes", "thermal_minutes",
    ],
}

# Minimum rows before we train a component model (avoid overfitting tiny fleets).
MIN_TRAIN_ROWS = 20
# Anomaly score (0-100) above which we fire an alert.
DEFAULT_ANOMALY_THRESHOLD = 80.0
# Don't re-fire the same vehicle+component within this window.
FIRE_DEDUP_SECONDS = 24 * 3600

_task: Optional[asyncio.Task] = None
_fired_at: dict[tuple[int, str], datetime] = {}


# ── Model persistence ─────────────────────────────────────────────────────────
def _model_path(component: str) -> Path:
    return MODEL_DIR / f"isolation_{component}.joblib"


def _save_model(component: str, model: IsolationForest, version: int) -> None:
    joblib.dump({"model": model, "version": version, "trained_at": datetime.now(timezone.utc).isoformat()},
                _model_path(component))


def _load_model(component: str) -> Optional[dict]:
    path = _model_path(component)
    if not path.exists():
        return None
    try:
        return joblib.load(path)
    except Exception as e:
        logger.warning("Failed to load model %s: %s", component, e)
        return None


# ── Training ──────────────────────────────────────────────────────────────────
def _feature_matrix(rows: list[VehicleFeature], columns: list[str]) -> np.ndarray:
    """Build a numeric matrix from feature rows, dropping rows with any null
    in the requested columns."""
    matrix = []
    for r in rows:
        vec = [getattr(r, c) for c in columns]
        if any(v is None for v in vec):
            continue
        matrix.append([float(v) for v in vec])
    return np.array(matrix, dtype=float) if matrix else np.empty((0, len(columns)))


async def _train_component(session: AsyncSession, component: str) -> bool:
    """Train (or retrain) one component's Isolation Forest on the fleet."""
    columns = COMPONENT_FEATURES[component]
    rows = list((await session.execute(
        select(VehicleFeature).order_by(VehicleFeature.date)
    )).scalars().all())
    X = _feature_matrix(rows, columns)
    if X.shape[0] < MIN_TRAIN_ROWS:
        logger.info("Model %s: only %d rows (< %d) — collecting data, not training",
                    component, X.shape[0], MIN_TRAIN_ROWS)
        return False

    model = IsolationForest(
        n_estimators=100,
        contamination=0.05,   # assume ~5% of fleet-days are anomalous
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X)
    version = (await session.execute(
        select(FailureEvent.id).order_by(FailureEvent.id.desc()).limit(1)
    )).scalar() or 0
    _save_model(component, model, int(version))
    logger.info("Model %s trained on %d rows (version %d)", component, X.shape[0], version)
    return True


# ── Scoring ───────────────────────────────────────────────────────────────────
def _anomaly_score(model: IsolationForest, vec: list[float]) -> float:
    """Convert IsolationForest decision_function to a 0-100 anomaly score.
    decision_function: higher = more normal. We invert and normalize."""
    x = np.array([vec], dtype=float)
    decision = float(model.decision_function(x)[0])
    # decision is roughly in [-0.5, 0.5] for typical contamination; map to 0-100.
    # More negative = more anomalous → higher score.
    score = 100.0 * (0.5 - decision) / 1.0
    return max(0.0, min(100.0, score))


async def score_vehicle(session: AsyncSession, vehicle_id: int) -> dict[str, float]:
    """Score a vehicle's latest feature row per component. Returns
    {component: anomaly_score} for components with a trained model + data."""
    latest = (await session.execute(
        select(VehicleFeature)
        .where(VehicleFeature.vehicle_id == vehicle_id)
        .order_by(VehicleFeature.date.desc())
        .limit(1)
    )).scalar_one_or_none()
    if latest is None:
        return {}

    out: dict[str, float] = {}
    for component, columns in COMPONENT_FEATURES.items():
        loaded = _load_model(component)
        if loaded is None:
            continue
        vec = [getattr(latest, c) for c in columns]
        if any(v is None for v in vec):
            continue
        out[component] = round(_anomaly_score(loaded["model"], [float(v) for v in vec]), 1)
    return out


# ── Firing (shadow mode) ──────────────────────────────────────────────────────
async def _ensure_anomaly_rule(session: AsyncSession, component: str) -> Rule:
    key = f"ml_anomaly_{component}"
    rule = (await session.execute(select(Rule).where(Rule.key == key))).scalar_one_or_none()
    if rule:
        return rule
    names = {
        "battery": "Battery behavior drifting from fleet norm",
        "cooling": "Cooling behavior drifting from fleet norm",
        "oil": "Oil system behavior drifting from fleet norm",
        "engine": "Engine behavior drifting from fleet norm",
    }
    recs = {
        "battery": "This car's battery behavior is statistically unusual vs the fleet. "
                   "Have the battery and charging system tested.",
        "cooling": "This car's cooling behavior is statistically unusual vs the fleet. "
                   "Check the thermostat, radiator, and coolant level.",
        "oil": "This car's oil system behavior is statistically unusual vs the fleet. "
               "Check oil level, condition, and for leaks.",
        "engine": "This car's engine behavior is statistically unusual vs the fleet. "
                  "A diagnostic scan is recommended.",
    }
    rule = Rule(
        key=key,
        name=names[component],
        description=f"Unsupervised anomaly detection ({component})",
        rule_type=RuleType.ANOMALY,
        sensor_type=f"ml_{component}",
        operator=">",
        threshold_value=DEFAULT_ANOMALY_THRESHOLD,
        duration_seconds=0,
        severity=Severity.WARNING,
        priority=WorkOrderPriority.MEDIUM,
        auto_work_order=True,
        recommendation=recs[component],
        is_active=True,
    )
    session.add(rule)
    await session.flush()
    invalidate_rules_cache()
    return rule


async def _maybe_fire(session: AsyncSession, vehicle: Vehicle, component: str,
                      score: float) -> None:
    threshold = await settings_store.get_float(
        f"ml.anomaly_threshold_{component}", DEFAULT_ANOMALY_THRESHOLD)
    if score < threshold:
        return
    key = (vehicle.id, component)
    last = _fired_at.get(key)
    if last and (datetime.now(timezone.utc) - last).total_seconds() < FIRE_DEDUP_SECONDS:
        return
    _fired_at[key] = datetime.now(timezone.utc)

    rule = await _ensure_anomaly_rule(session, component)
    await fire_rule(
        session,
        vehicle_id=vehicle.id,
        vehicle_name=vehicle.name,
        rule=rule,
        title=rule.name,
        message=f"Anomaly score {score:.0f}/100 for {component} "
                f"(threshold {threshold:.0f}) — statistically unusual vs the fleet.",
        trigger_value=round(score, 1),
        severity=Severity.WARNING,
    )
    logger.info("ML anomaly fired: %s → %s (score %.1f)", component, vehicle.name, score)


# ── Failure labels on feature rows ────────────────────────────────────────────
LABEL_LOOKBACK_DAYS = 14


async def label_failure_features(
    session: AsyncSession,
    *,
    vehicle_id: int,
    component: str,
    occurred_at: datetime,
    lookback_days: int = LABEL_LOOKBACK_DAYS,
) -> int:
    """Mark VehicleFeature rows in the lookback window as failed for evaluation."""
    if component not in COMPONENT_FEATURES:
        return 0
    start = occurred_at.date() - timedelta(days=lookback_days)
    end = occurred_at.date()
    rows = list((await session.execute(
        select(VehicleFeature).where(
            VehicleFeature.vehicle_id == vehicle_id,
            VehicleFeature.date >= start,
            VehicleFeature.date < end,
        )
    )).scalars().all())
    for r in rows:
        r.failed = True
        r.failure_component = component
    return len(rows)


async def _sync_failure_labels(session: AsyncSession) -> int:
    """Ensure all FailureEvents have labeled feature rows (idempotent)."""
    failures = list((await session.execute(select(FailureEvent))).scalars().all())
    n = 0
    for f in failures:
        n += await label_failure_features(
            session,
            vehicle_id=f.vehicle_id,
            component=f.component,
            occurred_at=f.occurred_at,
        )
    return n


async def _component_threshold(component: str) -> float:
    return await settings_store.get_float(
        f"ml.anomaly_threshold_{component}", DEFAULT_ANOMALY_THRESHOLD,
    )


# ── Evaluation (precision/recall vs labeled failures) ─────────────────────────
async def evaluate(session: AsyncSession) -> dict[str, Any]:
    """Score each trained model against labeled failures. Returns per-component
    precision/recall. No-op until FailureEvents exist."""
    failures = list((await session.execute(select(FailureEvent))).scalars().all())
    if not failures:
        return {"status": "no_failures_yet", "detail": "Collect failure labels via work-order completion"}

    await _sync_failure_labels(session)
    await session.flush()

    results: dict[str, dict] = {}
    for component in COMPONENT_FEATURES:
        loaded = _load_model(component)
        if loaded is None:
            results[component] = {"status": "not_trained"}
            continue
        threshold = await _component_threshold(component)
        tp = fp = fn = 0
        for f in failures:
            if f.component != component:
                continue
            # Prefer explicitly labeled rows; fall back to date window.
            rows = list((await session.execute(
                select(VehicleFeature).where(
                    VehicleFeature.vehicle_id == f.vehicle_id,
                    VehicleFeature.failed.is_(True),
                    VehicleFeature.failure_component == component,
                )
            )).scalars().all())
            if not rows:
                rows = list((await session.execute(
                    select(VehicleFeature).where(
                        VehicleFeature.vehicle_id == f.vehicle_id,
                        VehicleFeature.date >= (f.occurred_at.date() - timedelta(days=LABEL_LOOKBACK_DAYS)),
                        VehicleFeature.date < f.occurred_at.date(),
                    )
                )).scalars().all())
            flagged = any(
                _anomaly_score(loaded["model"], [float(getattr(r, c)) for c in COMPONENT_FEATURES[component]])
                >= threshold
                for r in rows
                if all(getattr(r, c) is not None for c in COMPONENT_FEATURES[component])
            )
            if flagged:
                tp += 1
            else:
                fn += 1
        # False positives: unlabeled (or non-failed) rows scored above threshold.
        all_rows = list((await session.execute(select(VehicleFeature))).scalars().all())
        failed_vehicles = {f.vehicle_id for f in failures if f.component == component}
        for r in all_rows:
            if r.vehicle_id in failed_vehicles:
                continue
            if r.failed and r.failure_component == component:
                continue
            vec = [getattr(r, c) for c in COMPONENT_FEATURES[component]]
            if any(v is None for v in vec):
                continue
            if _anomaly_score(loaded["model"], [float(v) for v in vec]) >= threshold:
                fp += 1
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        results[component] = {
            "status": "evaluated",
            "true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": round(precision, 3), "recall": round(recall, 3),
            "threshold": threshold,
        }
    return {"status": "ok", "results": results}


# ── Job entry points ──────────────────────────────────────────────────────────
async def run_once() -> dict[str, Any]:
    """Train any untrained models, then score every vehicle and fire anomalies."""
    async with async_session_factory() as session:
        # Train models that don't exist yet (or retrain if no model file).
        trained = {}
        for component in COMPONENT_FEATURES:
            if _load_model(component) is None:
                trained[component] = await _train_component(session, component)
        await session.commit()

        vehicles = list((await session.execute(select(Vehicle))).scalars().all())
        fired = 0
        for v in vehicles:
            scores = await score_vehicle(session, v.id)
            for component, score in scores.items():
                await _maybe_fire(session, v, component, score)
            await session.commit()
        logger.info("ML models: trained=%s, scored %d vehicle(s), fired=%d",
                    trained, len(vehicles), fired)
        return {"trained": trained, "vehicles": len(vehicles), "fired": fired}


async def _loop() -> None:
    await asyncio.sleep(180)   # let startup + features settle
    while True:
        try:
            await run_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("ML models job failed: %s", e)
        await asyncio.sleep(settings.PREDICTOR_INTERVAL_SECONDS)


def start() -> None:
    global _task
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None