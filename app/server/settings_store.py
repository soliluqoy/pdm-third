"""
PREDICT — settings store: DB-backed key/value config with a short in-process
cache (the ingest hot path reads these per packet).

Notable keys:
  ask_me_first            "true"/"false" — shadow mode: detections draft
                          SUGGESTED tasks for review instead of going to To-Do
  behavior.speed_limit_kmh, behavior.idle_minutes, behavior.accel_threshold_ms2,
  behavior.high_rpm_threshold, behavior.score_weights (JSON)
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

from sqlalchemy import select

from server.db import async_session_factory
from server.models import Setting

DEFAULTS: dict[str, str] = {
    "ask_me_first": "true",
    "behavior.speed_limit_kmh": "120",
    "behavior.idle_minutes": "5",
    "behavior.accel_threshold_ms2": "3.0",
    "behavior.high_rpm_threshold": "4000",
    "behavior.score_weights": json.dumps({
        "harsh_accel": 8, "harsh_brake": 10, "harsh_corner": 8,
        "speeding": 6, "idling": 3, "high_rpm": 4,
    }),
    # Predictive maintenance engine (physics / heuristic models)
    "predict.brake_pad_capacity_mj": "80",
    "predict.brake_decel_g": "0.25",
    "predict.battery_warn_rul_days": "30",
    "predict.oil_interval_km": "10000",
    "predict.mass_kg_default": "1500",
}

DESCRIPTIONS: dict[str, str] = {
    "ask_me_first": "Review suggested repairs before they go on your to-do list",
    "behavior.speed_limit_kmh": "Speeding threshold (km/h)",
    "behavior.idle_minutes": "Minutes of idling before it counts as an event",
    "behavior.accel_threshold_ms2": "Harsh accel/brake sensitivity (m/s², lower = more sensitive)",
    "behavior.high_rpm_threshold": "High-RPM threshold",
    "behavior.score_weights": "Driving-score penalty weights per event type (JSON)",
    "predict.brake_pad_capacity_mj": "Brake pad energy budget (MJ) before 0% life",
    "predict.brake_decel_g": "Min deceleration (g) counted as a braking wear event",
    "predict.battery_warn_rul_days": "Fire battery prediction when RUL falls below this",
    "predict.oil_interval_km": "Baseline oil-change interval (km)",
    "predict.mass_kg_default": "Default vehicle mass (kg) when not set on the car",
}

_cache: dict[str, str] = {}
_loaded_at: float = 0.0
_TTL = 15.0


async def _refresh() -> None:
    global _loaded_at
    async with async_session_factory() as session:
        rows = (await session.execute(select(Setting.key, Setting.value))).all()
    _cache.clear()
    _cache.update(DEFAULTS)
    _cache.update({k: v for k, v in rows})
    _loaded_at = time.monotonic()


async def get(key: str, default: Optional[str] = None) -> Optional[str]:
    if time.monotonic() - _loaded_at > _TTL:
        try:
            await _refresh()
        except Exception:
            if not _cache:          # DB not up yet; fall back to defaults
                _cache.update(DEFAULTS)
    return _cache.get(key, default if default is not None else DEFAULTS.get(key))


async def get_bool(key: str, default: bool = False) -> bool:
    return ((await get(key)) or ("true" if default else "false")).lower() == "true"


async def get_float(key: str, default: float) -> float:
    try:
        return float(await get(key) or default)
    except (TypeError, ValueError):
        return default


async def get_json(key: str, default: Any) -> Any:
    raw = await get(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


async def set_value(key: str, value: str) -> None:
    async with async_session_factory() as session:
        row = await session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value,
                          description=DESCRIPTIONS.get(key, ""))
            session.add(row)
        else:
            row.value = value
        await session.commit()
    _cache[key] = value


async def all_settings() -> dict[str, str]:
    await get("ask_me_first")   # ensure cache warm
    return dict(_cache)
