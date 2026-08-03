"""PREDICT v3 — rules: list presets, toggle on/off, edit threshold/duration.
No rule-builder UI by design — presets cover the fleet; new rules are code."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.api.deps import SessionDep
from server.models import Rule
from server.rules import invalidate_rules_cache
from server.schemas import RulePatch

router = APIRouter(prefix="/rules", tags=["rules"])


def _rule_dict(r: Rule) -> dict:
    return {
        "id": r.id, "key": r.key, "name": r.name, "description": r.description,
        "rule_type": r.rule_type.value, "vehicle_id": r.vehicle_id,
        "sensor_type": r.sensor_type, "operator": r.operator,
        "threshold_value": r.threshold_value, "duration_seconds": r.duration_seconds,
        "dtc_code": r.dtc_code, "interval_value": r.interval_value,
        "interval_unit": r.interval_unit,
        "severity": r.severity.value, "recommendation": r.recommendation,
        "auto_work_order": r.auto_work_order, "priority": r.priority.value,
        "is_active": r.is_active,
    }


@router.get("")
async def list_rules(session: AsyncSession = SessionDep):
    rows = (await session.execute(
        select(Rule).order_by(Rule.severity, Rule.name)
    )).scalars().all()
    return [_rule_dict(r) for r in rows]


@router.patch("/{rule_id}")
async def patch_rule(rule_id: int, body: RulePatch, session: AsyncSession = SessionDep):
    r = await session.get(Rule, rule_id)
    if r is None:
        raise HTTPException(404, "Rule not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(r, field, value)
    await session.commit()
    invalidate_rules_cache()
    return _rule_dict(r)