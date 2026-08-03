"""
PREDICT — database bootstrap (idempotent).

- tables via SQLAlchemy metadata
- TimescaleDB: hypertable, compression (7d), retention (configurable),
  continuous aggregates sensor_readings_1m / _1h / _1d with refresh policies
  (1m feeds ≤6 h charts, 1h feeds ≤30 d charts, 1d feeds all-time charts)
- preset rules + default settings
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import settings
from server.db import Base, engine
from server import models  # noqa: F401 — register metadata
from server.rules import seed_presets

logger = logging.getLogger("predict.init_db")


_TIMESCALE_SQL = """
DO $$ BEGIN
    PERFORM create_hypertable('sensor_readings', 'timestamp',
        if_not_exists => TRUE, migrate_data => TRUE);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

ALTER TABLE sensor_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'vehicle_id, sensor_type',
    timescaledb.compress_orderby = 'timestamp DESC'
);

DO $$ BEGIN
    PERFORM add_compression_policy('sensor_readings', INTERVAL '{compress_days} days',
        if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

DO $$ BEGIN
    PERFORM add_retention_policy('sensor_readings', INTERVAL '{retention_days} days',
        if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_readings_1m
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 minute', "timestamp") AS bucket,
       vehicle_id, sensor_type,
       avg(value) AS avg_value, min(value) AS min_value,
       max(value) AS max_value, count(*)::int AS samples
FROM sensor_readings
GROUP BY bucket, vehicle_id, sensor_type
WITH NO DATA;

DO $$ BEGIN
    PERFORM add_continuous_aggregate_policy('sensor_readings_1m',
        start_offset => INTERVAL '3 hours', end_offset => INTERVAL '5 minutes',
        schedule_interval => INTERVAL '5 minutes', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_readings_1h
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 hour', "timestamp") AS bucket,
       vehicle_id, sensor_type,
       avg(value) AS avg_value, min(value) AS min_value,
       max(value) AS max_value, count(*)::int AS samples
FROM sensor_readings
GROUP BY bucket, vehicle_id, sensor_type
WITH NO DATA;

DO $$ BEGIN
    PERFORM add_continuous_aggregate_policy('sensor_readings_1h',
        start_offset => INTERVAL '30 days', end_offset => INTERVAL '1 hour',
        schedule_interval => INTERVAL '1 hour', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

CREATE MATERIALIZED VIEW IF NOT EXISTS sensor_readings_1d
WITH (timescaledb.continuous) AS
SELECT time_bucket('1 day', "timestamp") AS bucket,
       vehicle_id, sensor_type,
       avg(value) AS avg_value, min(value) AS min_value,
       max(value) AS max_value, count(*)::int AS samples
FROM sensor_readings
GROUP BY bucket, vehicle_id, sensor_type
WITH NO DATA;

DO $$ BEGIN
    PERFORM add_continuous_aggregate_policy('sensor_readings_1d',
        start_offset => INTERVAL '400 days', end_offset => INTERVAL '1 day',
        schedule_interval => INTERVAL '1 day', if_not_exists => TRUE);
EXCEPTION WHEN OTHERS THEN NULL;
END $$;
"""


def _split_sql(script: str) -> list[str]:
    """Split a script on ';' — but not inside $$ … $$ dollar-quoted blocks."""
    statements: list[str] = []
    current: list[str] = []
    in_dollar = False
    i = 0
    while i < len(script):
        if script.startswith("$$", i):
            in_dollar = not in_dollar
            current.append("$$")
            i += 2
            continue
        ch = script[i]
        if ch == ";" and not in_dollar:
            statements.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    tail = "".join(current)
    if tail.strip():
        statements.append(tail)
    return [s.strip() for s in statements if s.strip()]


# Idempotent column adds for existing deployments (create_all won't ALTER).
_VEHICLE_COLUMN_MIGRATIONS = """
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS mass_kg DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS oil_capacity_l DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS brake_pad_capacity_mj DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS last_oil_change_at TIMESTAMPTZ;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS last_oil_change_odo DOUBLE PRECISION;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS last_brake_service_at TIMESTAMPTZ;
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS last_brake_service_odo DOUBLE PRECISION;
"""


async def init_db() -> None:
    logger.info("Initializing database schema…")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
        await conn.run_sync(Base.metadata.create_all)
        for statement in _split_sql(_VEHICLE_COLUMN_MIGRATIONS):
            await conn.execute(text(statement))
        script = _TIMESCALE_SQL.format(
            compress_days=settings.COMPRESS_AFTER_DAYS,
            retention_days=settings.READINGS_RETENTION_DAYS,
        )
        for statement in _split_sql(script):
            await conn.execute(text(statement))
    logger.info("Schema ready (hypertable + aggregates + lifecycle policies)")

    async with AsyncSession(engine) as session:
        await seed_presets(session)
