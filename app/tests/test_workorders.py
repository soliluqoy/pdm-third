"""Work order lifecycle: suggested → open → in_progress → done,
and the immutable maintenance_log write on completion.

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

from server import workorders
from server.db import Base
from server.models import (
    Alert, AlertStatus, MaintenanceLog, Severity, Vehicle, WorkOrder,
    WorkOrderPriority, WorkOrderStatus,
)

TABLES = [Vehicle.__table__, Alert.__table__, WorkOrder.__table__,
          MaintenanceLog.__table__]


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        for t in TABLES:
            await conn.run_sync(t.create, checkfirst=True)
    async with AsyncSession(engine) as s:
        yield s
    await engine.dispose()


async def _car(session: AsyncSession) -> Vehicle:
    v = Vehicle(name="Test Car", imei="352999001234567", device_type="fmc150")
    session.add(v)
    await session.commit()
    return v


@pytest.mark.asyncio
class TestLifecycle:
    async def test_full_happy_path(self, session):
        v = await _car(session)
        alert = Alert(vehicle_id=v.id, severity=Severity.WARNING,
                      status=AlertStatus.ACTIVE, title="Battery low",
                      message="11.4 V", dedup_key=f"0:{v.id}")
        session.add(alert)
        await session.flush()

        wo = await workorders.draft_from_rule(
            session, vehicle_id=v.id, alert=alert, title="Battery low",
            description="Test the battery", priority=WorkOrderPriority.HIGH,
            shadow_mode=True)
        await session.commit()
        assert wo.status == WorkOrderStatus.SUGGESTED
        assert alert.work_order_id == wo.id

        assert (await workorders.approve(session, wo.id)).status == WorkOrderStatus.OPEN
        assert (await workorders.start(session, wo.id)).status == WorkOrderStatus.IN_PROGRESS
        done = await workorders.complete(session, wo.id, completion_notes="New battery",
                                         cost=120.0, odometer=61234.0)
        await session.commit()
        assert done.status == WorkOrderStatus.DONE

        # maintenance_log row written
        history = await workorders.maintenance_history(session, vehicle_id=v.id)
        assert len(history) == 1
        assert history[0].title == "Battery low"
        assert float(history[0].cost) == 120.0
        assert history[0].odometer == 61234.0

        # linked alert auto-resolved
        await session.refresh(alert)
        assert alert.status == AlertStatus.RESOLVED

    async def test_shadow_off_goes_straight_to_open(self, session):
        v = await _car(session)
        alert = Alert(vehicle_id=v.id, severity=Severity.INFO,
                      status=AlertStatus.ACTIVE, title="Service due",
                      message="500 km left", dedup_key=f"0:{v.id}")
        session.add(alert)
        await session.flush()
        wo = await workorders.draft_from_rule(
            session, vehicle_id=v.id, alert=alert, title="Service due",
            description="Book service", priority=WorkOrderPriority.LOW,
            shadow_mode=False)
        assert wo.status == WorkOrderStatus.OPEN

    async def test_illegal_transitions_rejected(self, session):
        v = await _car(session)
        wo = await workorders.create_manual(session, vehicle_id=v.id, title="Wipers")
        await session.commit()
        assert wo.status == WorkOrderStatus.OPEN
        assert await workorders.approve(session, wo.id) is None      # not suggested
        assert await workorders.complete(session, wo.id) is not None  # open → done ok

        await session.commit()
        assert await workorders.start(session, wo.id) is None        # already done
        assert await workorders.cancel(session, wo.id) is None       # already done

    async def test_cancel_suggestion(self, session):
        v = await _car(session)
        alert = Alert(vehicle_id=v.id, severity=Severity.INFO,
                      status=AlertStatus.ACTIVE, title="FYI", message="m",
                      dedup_key=f"0:{v.id}")
        session.add(alert)
        await session.flush()
        wo = await workorders.draft_from_rule(
            session, vehicle_id=v.id, alert=alert, title="FYI",
            description=None, priority=WorkOrderPriority.LOW, shadow_mode=True)
        await session.commit()
        assert (await workorders.cancel(session, wo.id)).status == WorkOrderStatus.CANCELLED

    async def test_counts_and_csv(self, session):
        v = await _car(session)
        w1 = await workorders.create_manual(session, vehicle_id=v.id, title="Oil")
        await workorders.create_manual(session, vehicle_id=v.id, title="Tires")
        await session.commit()
        counts = await workorders.count_open_by_vehicle(session)
        assert counts[v.id] == 2

        await workorders.start(session, w1.id)
        await workorders.complete(session, w1.id, cost=55.5, odometer=100.0)
        await session.commit()
        csv_text = await workorders.maintenance_csv(session, vehicle_id=v.id)
        assert "Oil" in csv_text
        assert "55.5" in csv_text
        assert "Tires" not in csv_text  # not completed → not in history
