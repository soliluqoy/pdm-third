"""
PREDICT v3 — data model.

Flow:  SensorReading → Rule → Alert → WorkOrder → MaintenanceLog
       Telemetry → Trip → DrivingEvent → DriverScore
       Nightly → SensorBaseline (+ anomaly Alerts)
       Setting = key/value config (ask_me_first, behavior thresholds, …)

v3 vs v2: "Issue" is now "Alert" (with dedup_key + occurrence_count),
"RepairTask" is now "WorkOrder" with a real lifecycle
(suggested → open → in_progress → done | cancelled, cost, odometer).
MaintenanceLog stays immutable and append-only.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from server.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── Enums ─────────────────────────────────────────────────────────────────────
class Health(str, enum.Enum):
    GREEN = "green"      # all good
    YELLOW = "yellow"    # check soon
    RED = "red"          # urgent
    GREY = "grey"        # offline / unknown


class Severity(str, enum.Enum):
    CRITICAL = "critical"   # UI: "Urgent"
    WARNING = "warning"     # UI: "Check soon"
    INFO = "info"           # UI: "FYI"


class AlertStatus(str, enum.Enum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class WorkOrderStatus(str, enum.Enum):
    SUGGESTED = "suggested"     # created by automation, awaiting review (shadow mode)
    OPEN = "open"               # on the list
    IN_PROGRESS = "in_progress"  # work started
    DONE = "done"
    CANCELLED = "cancelled"     # dismissed suggestion / cancelled planned work


class WorkOrderSource(str, enum.Enum):
    AUTO = "auto"      # drafted by a firing rule
    MANUAL = "manual"  # created by the owner


class WorkOrderPriority(str, enum.Enum):
    URGENT = "urgent"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RuleType(str, enum.Enum):
    THRESHOLD = "threshold"    # sensor value vs limit
    DTC = "dtc"                # diagnostic trouble code match ("" = any)
    SCHEDULED = "scheduled"    # odometer / engine-hours / days interval
    BEHAVIOR = "behavior"      # daily driving-event count
    ANOMALY = "anomaly"        # deviation from this car's own baseline


class DrivingEventType(str, enum.Enum):
    HARSH_ACCEL = "harsh_accel"
    HARSH_BRAKE = "harsh_brake"
    HARSH_CORNER = "harsh_corner"
    SPEEDING = "speeding"
    IDLING = "idling"
    HIGH_RPM = "high_rpm"


class DrivingEventSource(str, enum.Enum):
    DEVICE = "device"    # tracker's own eco-driving detection (accurate)
    DERIVED = "derived"  # server-side estimate from 10 s samples


# =============================================================================
# Car
# =============================================================================
class Vehicle(Base):
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    license_plate = Column(String(20))
    make = Column(String(50))
    model = Column(String(50))
    year = Column(Integer)
    vin = Column(String(50))
    imei = Column(String(20), unique=True, nullable=False, index=True)
    device_type = Column(String(20), nullable=False, default="fmc001")  # fmc001 | fmc150
    sim_phone = Column(String(32))

    health = Column(SAEnum(Health), nullable=False, default=Health.GREY, index=True)
    last_seen = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)


class SensorReading(Base):
    """Time-series; converted to a TimescaleDB hypertable at startup.
    Composite PK (timestamp, id) — Timescale requires the partition column in
    the primary key."""
    __tablename__ = "sensor_readings"

    timestamp = Column(DateTime(timezone=True), nullable=False, primary_key=True)
    id = Column(Integer, primary_key=True, autoincrement=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    sensor_type = Column(String(50), nullable=False)
    value = Column(Float, nullable=False)
    unit = Column(String(20))
    latitude = Column(Float)
    longitude = Column(Float)
    speed = Column(Float)
    ignition = Column(Boolean)

    __table_args__ = (
        Index("ix_readings_vehicle_sensor_time", "vehicle_id", "sensor_type", "timestamp"),
    )


# =============================================================================
# Rules → Alerts → Work Orders → History
# =============================================================================
class Rule(Base):
    """A detection rule. Seeded with owner-friendly presets; Settings exposes
    only the threshold + on/off — no rule-builder UI."""
    __tablename__ = "rules"

    id = Column(Integer, primary_key=True)
    key = Column(String(50), unique=True, nullable=False)   # stable preset id
    name = Column(String(120), nullable=False)
    description = Column(Text)
    rule_type = Column(SAEnum(RuleType), nullable=False, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=True, index=True)          # NULL = every car

    sensor_type = Column(String(50))    # threshold/anomaly target; behavior: event type
    operator = Column(String(10))       # > < >= <= ==
    threshold_value = Column(Float)
    duration_seconds = Column(Integer, default=0)   # must hold this long
    dtc_code = Column(String(20))       # DTC rules; "" / NULL = any code
    interval_value = Column(Float)      # scheduled: km, engine hours or days
    interval_unit = Column(String(20))  # km | engine_hours | days

    severity = Column(SAEnum(Severity), nullable=False, default=Severity.WARNING)
    # Plain-language advice. When set + auto_work_order, firing drafts a WorkOrder.
    recommendation = Column(Text)
    auto_work_order = Column(Boolean, nullable=False, default=False)
    priority = Column(SAEnum(WorkOrderPriority), default=WorkOrderPriority.MEDIUM)
    is_active = Column(Boolean, nullable=False, default=True, index=True)


class Alert(Base):
    """Something PREDICT noticed. dedup_key (rule_id:vehicle_id) means a
    re-firing rule bumps occurrence_count on the still-active alert instead of
    spamming new rows. Alerts are never deleted — this table IS the alert
    history."""
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    rule_id = Column(Integer, ForeignKey("rules.id", ondelete="SET NULL"), nullable=True)
    severity = Column(SAEnum(Severity), nullable=False, index=True)
    status = Column(SAEnum(AlertStatus), nullable=False,
                    default=AlertStatus.ACTIVE, index=True)
    title = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    trigger_value = Column(Float)
    trigger_timestamp = Column(DateTime(timezone=True), default=utcnow)
    dedup_key = Column(String(80), index=True)          # "rule_id:vehicle_id"
    occurrence_count = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    resolved_at = Column(DateTime(timezone=True))
    work_order_id = Column(Integer, ForeignKey("work_orders.id", ondelete="SET NULL",
                                               use_alter=True, name="fk_alerts_work_order_id"),
                           nullable=True)

    __table_args__ = (
        Index("ix_alerts_vehicle_status", "vehicle_id", "status"),
    )


class WorkOrder(Base):
    """A maintenance to-do with a full lifecycle. SUGGESTED = shadow mode: the
    system drafted it, waiting for approve/dismiss. Completing writes an
    immutable MaintenanceLog row."""
    __tablename__ = "work_orders"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    alert_id = Column(Integer, ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True)

    title = Column(String(200), nullable=False)
    description = Column(Text)
    priority = Column(SAEnum(WorkOrderPriority), nullable=False,
                      default=WorkOrderPriority.MEDIUM, index=True)
    status = Column(SAEnum(WorkOrderStatus), nullable=False,
                    default=WorkOrderStatus.SUGGESTED, index=True)
    source = Column(SAEnum(WorkOrderSource), nullable=False,
                    default=WorkOrderSource.AUTO)

    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    due_date = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    completion_notes = Column(Text)
    cost = Column(Numeric(10, 2))
    odometer_at_completion = Column(Float)

    __table_args__ = (
        Index("ix_work_orders_vehicle_status", "vehicle_id", "status"),
    )


class MaintenanceLog(Base):
    """Immutable, append-only history of completed work. Never deleted."""
    __tablename__ = "maintenance_log"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    work_order_id = Column(Integer, ForeignKey("work_orders.id", ondelete="SET NULL"),
                           nullable=True)
    event_type = Column(String(50), nullable=False, default="repair")  # repair | inspection | note
    title = Column(String(200), nullable=False)
    notes = Column(Text)
    cost = Column(Numeric(10, 2))
    odometer = Column(Float)
    event_date = Column(DateTime(timezone=True), nullable=False, default=utcnow, index=True)


class DtcEvent(Base):
    """Every diagnostic trouble code seen, matched by a rule or not."""
    __tablename__ = "dtc_events"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    timestamp = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    dtc_code = Column(String(20), nullable=False, index=True)
    alert_id = Column(Integer, ForeignKey("alerts.id", ondelete="SET NULL"), nullable=True)

    __table_args__ = (
        Index("ix_dtc_vehicle_time", "vehicle_id", "timestamp"),
    )


class HealthEvent(Base):
    """Health color transitions (for the car timeline)."""
    __tablename__ = "health_events"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    from_health = Column(SAEnum(Health), nullable=False)
    to_health = Column(SAEnum(Health), nullable=False)
    reason = Column(String(100))
    timestamp = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        Index("ix_health_events_vehicle_time", "vehicle_id", "timestamp"),
    )


# =============================================================================
# Driving
# =============================================================================
class Trip(Base):
    __tablename__ = "trips"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    start_ts = Column(DateTime(timezone=True), nullable=False)
    end_ts = Column(DateTime(timezone=True))
    start_odometer = Column(Float)
    end_odometer = Column(Float)
    distance_km = Column(Float)
    duration_seconds = Column(Integer)
    max_speed = Column(Float)
    avg_speed = Column(Float)
    fuel_start = Column(Float)
    fuel_end = Column(Float)
    idle_seconds = Column(Integer, nullable=False, default=0)
    is_open = Column(Boolean, nullable=False, default=True, index=True)

    __table_args__ = (
        Index("ix_trips_vehicle_start", "vehicle_id", "start_ts"),
    )


class DrivingEvent(Base):
    __tablename__ = "driving_events"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    trip_id = Column(Integer, ForeignKey("trips.id", ondelete="SET NULL"), nullable=True)
    ts = Column(DateTime(timezone=True), nullable=False)
    event_type = Column(SAEnum(DrivingEventType), nullable=False, index=True)
    value = Column(Float)
    latitude = Column(Float)
    longitude = Column(Float)
    source = Column(SAEnum(DrivingEventSource), nullable=False,
                    default=DrivingEventSource.DERIVED)

    __table_args__ = (
        Index("ix_driving_events_vehicle_ts", "vehicle_id", "ts"),
    )


class DriverScore(Base):
    """Daily per-car driving score (0–100, higher = calmer driving)."""
    __tablename__ = "driver_scores"

    id = Column(Integer, primary_key=True)
    date = Column(Date, nullable=False)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    trips = Column(Integer, nullable=False, default=0)
    distance_km = Column(Float, nullable=False, default=0.0)
    events_per_100km = Column(JSONB, default=dict)
    idle_ratio = Column(Float, nullable=False, default=0.0)
    score = Column(Float, nullable=False, default=100.0)

    __table_args__ = (
        UniqueConstraint("date", "vehicle_id", name="uq_driver_scores_date_vehicle"),
    )


# =============================================================================
# Predictive maintenance + settings
# =============================================================================
class SensorBaseline(Base):
    """Per-car, per-sensor 30-day statistics (from hourly aggregates)."""
    __tablename__ = "sensor_baselines"

    id = Column(Integer, primary_key=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    sensor_type = Column(String(50), nullable=False)
    window = Column(String(20), nullable=False, default="30d")
    mean = Column(Float, nullable=False)
    std = Column(Float, nullable=False)
    p95 = Column(Float)
    sample_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        UniqueConstraint("vehicle_id", "sensor_type", "window",
                         name="uq_baselines_vehicle_sensor_window"),
    )


class Setting(Base):
    """Key/value config: ask_me_first (shadow mode), behavior thresholds, …"""
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    description = Column(Text)
    updated_at = Column(DateTime(timezone=True), nullable=False,
                        default=utcnow, onupdate=utcnow)
