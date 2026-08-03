"""
PREDICT v3 — settings: key/value config (ask-me-first, behavior thresholds),
device catalog, and tracker setup info.
(Rule tuning lives in api/rules.py — one source of truth.)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from server import settings_store
from server.catalog import DEVICE_MODELS, sensors_for_model
from server.config import settings
from server.schemas import SettingsPatch
from server.ws import hub

router = APIRouter(prefix="/settings", tags=["settings"])

# Only these keys are writable from the UI (guards against typos).
_WRITABLE = set(settings_store.DEFAULTS)


@router.get("")
async def get_settings_all():
    values = await settings_store.all_settings()
    return {
        "values": values,
        "descriptions": settings_store.DESCRIPTIONS,
        "tracker_public_host": settings.TRACKER_PUBLIC_HOST,
        "teltonika_port": settings.TELTONIKA_PORT,
        "device_models": DEVICE_MODELS,
    }


@router.patch("")
async def patch_settings(body: SettingsPatch):
    for key, value in body.values.items():
        if key not in _WRITABLE:
            raise HTTPException(400, f"Unknown setting {key!r}")
        await settings_store.set_value(key, str(value))
    await hub.broadcast("settings", {"values": body.values})
    return {"saved": list(body.values)}


# ── Device sensor catalog (what each tracker model can report) ────────────────
@router.get("/catalog/{model}")
async def device_catalog(model: str):
    if model not in DEVICE_MODELS:
        raise HTTPException(404, f"Unknown model {model!r} (have: {', '.join(DEVICE_MODELS)})")
    return {"model": model, "sensors": sensors_for_model(model)}
