"""Alert lifecycle: dedup (occurrence_count), auto-resolve, manual close.

Runs against in-memory SQLite — only the non-JSONB tables are created.
Requires greenlet (async SQLAlchemy); skipped where its C ext is unavailable.
"""
import pytest

try:
    import greenlet  # noqa: F401
    _GREENLET_OK = True
except Exception:
    _GREENLET_OK = False

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _GREENLET_OK,
                       reason="greenlet C extension unavailable (e.g. Python 3.14)"),
]

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from server import alerts
from server.db import Base
from server.models import (
    Alert, AlertStatus, Rule, RuleType, Severity, Vehicle, WorkOrderPriority,
)

TABLES = [Vehicle.__table__, Rule.__table__, Alert.__table__]


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        for t in TABLES:
            await conn.run_sync(t.create, checkfirst=True)
    async with AsyncSession(engine) as s:
        yield s
    await engine.dispose()


async def _seed(session: AsyncSession) -> tuple[Vehicle, Rule]:
    v = Vehicle(name="Test Car", imei="352999001234567", device_type="fmc150")
    r = Rule(
        key="overheat", name="Engine overheating", rule_type=RuleType.THRESHOLD,
        sensor_type="coolant_temperature", operator=">", threshold_value=110,
        severity=Severity.CRITICAL, priority=WorkOrderPriority.URGENT,
        auto_work_order=True, recommendation="Stop and cool down.", is_active=True,
    )
    session.add_all([v, r])
    await session.commit()
    return v, r


@pytest.mark.asyncio
class TestDedup:
    async def test_first_fire_creates_alert(self, session):
        v, r = await _seed(session)
        alert, created = await alerts.create_or_refresh(
            session, rule=r, vehicle_id=v.id, severity=r.severity,
            title=r.name, message="coolant 118", trigger_value=118.0)
        await session.commit()
        assert created is True
        assert alert.occurrence_count == 1
        assert alert.status == AlertStatus.ACTIVE
        assert alert.dedup_key == f"{r.id}:{v.id}"

    async def test_refire_bumps_count_not_new_row(self, session):
        v, r = await _seed(session)
        for temp in (118.0, 119.0, 120.0):
            _, created = await alerts.create_or_refresh(
                session, rule=r, vehicle_id=v.id, severity=r.severity,
                title=r.name, message=f"coolant {temp}", trigger_value=temp)
        await session.commit()
        assert created is False
        all_alerts = await alerts.list_alerts(session, status="all")
        assert len(all_alerts) == 1
        assert all_alerts[0].occurrence_count == 3
        assert all_alerts[0].trigger_value == 120.0  # latest value kept

    async def test_resolve_allows_new_alert(self, session):
        v, r = await _seed(session)
        a1, _ = await alerts.create_or_refresh(
            session, rule=r, vehicle_id=v.id, severity=r.severity,
            title=r.name, message="hot", trigger_value=118.0)
        await alerts.auto_resolve(session, rule=r, vehicle_id=v.id)
        await session.commit()
        a2, created2 = await alerts.create_or_refresh(
            session, rule=r, vehicle_id=v.id, severity=r.severity,
            title=r.name, message="hot again", trigger_value=119.0)
        await session.commit()
        assert created2 is True
        assert a2.id != a1.id
        history = await alerts.list_alerts(session, status="all")
        assert len(history) == 2


@pytest.mark.asyncio
class TestResolveDismiss:
    async def test_auto_resolve(self, session):
        v, r = await _seed(session)
        await alerts.create_or_refresh(
            session, rule=r, vehicle_id=v.id, severity=r.severity,
            title=r.name, message="hot", trigger_value=118.0)
        await session.commit()
        resolved = await alerts.auto_resolve(session, rule=r, vehicle_id=v.id)
        await session.commit()
        assert resolved is not None
        assert resolved.status == AlertStatus.RESOLVED
        assert resolved.resolved_at is not None
        # resolving again is a no-op
        assert await alerts.auto_resolve(session, rule=r, vehicle_id=v.id) is None

    async def test_manual_resolve_and_dismiss(self, session):
        v, r = await _seed(session)
        a1, _ = await alerts.create_or_refresh(
            session, rule=r, vehicle_id=v.id, severity=r.severity,
            title=r.name, message="hot", trigger_value=118.0)
        await session.commit()
        assert (await alerts.resolve(session, a1.id)).status == AlertStatus.RESOLVED
        assert await alerts.resolve(session, a1.id) is None  # already closed

        a2, _ = await alerts.create_or_refresh(
            session, rule=r, vehicle_id=v.id, severity=r.severity,
            title=r.name, message="hot", trigger_value=119.0)
        await session.commit()
        assert (await alerts.dismiss(session, a2.id)).status == AlertStatus.DISMISSED

    async def test_count_active_by_vehicle(self, session):
        v, r = await _seed(session)
        for i in range(2):
            await alerts.create_or_refresh(
                session, rule=None, vehicle_id=v.id, severity=Severity.WARNING,
                title=f"Ad-hoc {i}", message="m", dedup_key=f"adhoc:{i}:{v.id}")
        await session.commit()
        counts = await alerts.count_active_by_vehicle(session)
        assert counts[v.id]["warning"] == 2
        assert counts[v.id]["total"] == 2
