"""
PREDICT — predictive model API: model status, per-vehicle scores, evaluation.
ML anomaly scores only (higher = worse). PME health lives on /cars/.../prognostics.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from server import settings_store
from server.api.deps import SessionDep
from server.models import Vehicle
from server.services import models as models_service

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/status")
async def model_status():
    """List trained models, their versions, and whether they're ready."""
    out = {}
    for component, columns in models_service.COMPONENT_FEATURES.items():
        loaded = models_service._load_model(component)
        threshold = await settings_store.get_float(
            f"ml.anomaly_threshold_{component}",
            models_service.DEFAULT_ANOMALY_THRESHOLD,
        )
        if loaded is None:
            out[component] = {
                "status": "not_trained",
                "features": columns,
                "min_train_rows": models_service.MIN_TRAIN_ROWS,
                "threshold": threshold,
            }
        else:
            out[component] = {
                "status": "trained",
                "version": loaded["version"],
                "trained_at": loaded["trained_at"],
                "features": columns,
                "threshold": threshold,
            }
    return {"models": out}


@router.get("/evaluate")
async def evaluate(session: AsyncSession = SessionDep):
    """Precision/recall of each model against labeled failures.
    No-op until FailureEvents are recorded via work-order completion."""
    return await models_service.evaluate(session)


@router.get("/vehicles/{vehicle_id}")
async def vehicle_scores(vehicle_id: int, session: AsyncSession = SessionDep):
    """Per-component anomaly scores for one vehicle's latest feature row."""
    if await session.get(Vehicle, vehicle_id) is None:
        raise HTTPException(404, "Car not found")
    scores = await models_service.score_vehicle(session, vehicle_id)
    return {"vehicle_id": vehicle_id, "scores": scores}