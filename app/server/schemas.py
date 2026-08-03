"""PREDICT v3 — pydantic request/response schemas for the REST API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ── Cars ──────────────────────────────────────────────────────────────────────
class CarCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    imei: str = Field(min_length=14, max_length=20, pattern=r"^\d+$")
    device_type: str = Field(pattern=r"^(fmc001|fmc150)$")
    license_plate: Optional[str] = None
    sim_phone: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    vin: Optional[str] = None
    mass_kg: Optional[float] = Field(default=None, ge=500, le=50000)
    oil_capacity_l: Optional[float] = Field(default=None, ge=1, le=50)
    brake_pad_capacity_mj: Optional[float] = Field(default=None, ge=1, le=1000)
    last_oil_change_odo: Optional[float] = Field(default=None, ge=0)
    last_brake_service_odo: Optional[float] = Field(default=None, ge=0)


class CarUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    device_type: Optional[str] = Field(default=None, pattern=r"^(fmc001|fmc150)$")
    license_plate: Optional[str] = None
    sim_phone: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    vin: Optional[str] = None
    mass_kg: Optional[float] = Field(default=None, ge=500, le=50000)
    oil_capacity_l: Optional[float] = Field(default=None, ge=1, le=50)
    brake_pad_capacity_mj: Optional[float] = Field(default=None, ge=1, le=1000)
    last_oil_change_odo: Optional[float] = Field(default=None, ge=0)
    last_brake_service_odo: Optional[float] = Field(default=None, ge=0)


# ── Work orders ───────────────────────────────────────────────────────────────
class WorkOrderCreate(BaseModel):
    vehicle_id: int
    title: str = Field(min_length=1, max_length=200)
    description: Optional[str] = None
    priority: str = "medium"
    due_date: Optional[datetime] = None


class WorkOrderComplete(BaseModel):
    notes: Optional[str] = None
    cost: Optional[float] = Field(default=None, ge=0)
    odometer: Optional[float] = Field(default=None, ge=0)


# ── Settings ──────────────────────────────────────────────────────────────────
class SettingsPatch(BaseModel):
    values: dict[str, str]


class RulePatch(BaseModel):
    threshold_value: Optional[float] = None
    duration_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    is_active: Optional[bool] = None


# ── History ───────────────────────────────────────────────────────────────────
class HistoryPoint(BaseModel):
    ts: datetime
    value: float
    min_value: Optional[float] = None
    max_value: Optional[float] = None