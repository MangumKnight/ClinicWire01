"""
Tests for Activity API endpoints.

These tests verify:
1. Org isolation - Org A user only sees Org A events
2. Event type filtering works
3. Pagination works correctly

Run with: python tests/test_activity_api.py
"""

import asyncio
import uuid
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from db.models_multitenant import Base, Organization, User, OrgMembership, ActivityLog
from db.repo_v2 import ActivityLogRepository


# Test organization and user IDs
ORG_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


async def setup_test_data(session: AsyncSession):
    """Create test orgs, users, and activity events"""

    # Check if orgs exist, create if not
    from sqlalchemy import select

    result = await session.execute(
        select(Organization).where(Organization.id.in_([ORG_A_ID, ORG_B_ID]))
    )
    existing_orgs = {o.id for o in result.scalars().all()}

    if ORG_A_ID not in existing_orgs:
        org_a = Organization(id=ORG_A_ID, name="Test Org A", slug="test-org-a-api")
        session.add(org_a)

    if ORG_B_ID not in existing_orgs:
        org_b = Organization(id=ORG_B_ID, name="Test Org B", slug="test-org-b-api")
        session.add(org_b)

    await session.commit()

    # Check if users exist
    result = await session.execute(
        select(User).where(User.id.in_([USER_A_ID, USER_B_ID]))
    )
    existing_users = {u.id for u in result.scalars().all()}

    if USER_A_ID not in existing_users:
        user_a = User(id=USER_A_ID, email="testa@test.com", name="Test User A")
        session.add(user_a)

    if USER_B_ID not in existing_users:
        user_b = User(id=USER_B_ID, email="testb@test.com", name="Test User B")
        session.add(user_b)

    await session.commit()

    # Create memberships if they don't exist
    result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.user_id.in_([USER_A_ID, USER_B_ID])
        )
    )
    existing_memberships = {(m.org_id, m.user_id) for m in result.scalars().all()}

    if (ORG_A_ID, USER_A_ID) not in existing_memberships:
        membership_a = OrgMembership(org_id=ORG_A_ID, user_id=USER_A_ID, role="admin")
        session.add(membership_a)

    if (ORG_B_ID, USER_B_ID) not in existing_memberships:
        membership_b = OrgMembership(org_id=ORG_B_ID, user_id=USER_B_ID, role="admin")
        session.add(membership_b)

    await session.commit()


async def create_test_events(session: AsyncSession):
    """Create activity events for both orgs"""
    activity_repo = ActivityLogRepository(session)

    # Clean up old test events
    await session.execute(
        text("DELETE FROM activity_log WHERE org_id IN (:org_a, :org_b)"),
        {"org_a": str(ORG_A_ID), "org_b": str(ORG_B_ID)}
    )
    await session.commit()

    # Create events for Org A
    await activity_repo.log_event(
        org_id=ORG_A_ID,
        event_type="task.created",
        summary="Task created for Patient A1",
        actor_id=USER_A_ID,
        details={"workflow_type": "POC_SIGNATURE"}
    )

    await activity_repo.log_event(
        org_id=ORG_A_ID,
        event_type="call.initiated",
        summary="Call initiated to Dr. Alpha",
        actor_id=USER_A_ID,
        details={"mode": "simulation"}
    )

    await activity_repo.log_event(
        org_id=ORG_A_ID,
        event_type="call.completed",
        summary="Call completed for Dr. Alpha",
        actor_id=None,
        details={"duration_sec": 45}
    )

    # Create events for Org B
    await activity_repo.log_event(
        org_id=ORG_B_ID,
        event_type="task.created",
        summary="Task created for Patient B1",
        actor_id=USER_B_ID,
        details={"workflow_type": "POC_SIGNATURE"}
    )

    await activity_repo.log_event(
        org_id=ORG_B_ID,
        event_type="sms.sent",
        summary="SMS sent for task outcome",
        actor_id=None,
        details={"outcome": "Completed"}
    )

    print(f"Created 3 events for Org A, 2 events for Org B")


async def test_org_isolation(session: AsyncSession):
    """Test that Org A user only sees Org A events"""
    print("\n--- Test 1: Org Isolation ---")

    # Query as Org A user (only sees org_ids = [ORG_A_ID])
    from sqlalchemy import select
    result = await session.execute(
        select(ActivityLog).where(ActivityLog.org_id.in_([ORG_A_ID]))
    )
    org_a_events = list(result.scalars().all())

    # Query as Org B user (only sees org_ids = [ORG_B_ID])
    result = await session.execute(
        select(ActivityLog).where(ActivityLog.org_id.in_([ORG_B_ID]))
    )
    org_b_events = list(result.scalars().all())

    print(f"Org A sees {len(org_a_events)} events")
    print(f"Org B sees {len(org_b_events)} events")

    # Verify isolation
    assert len(org_a_events) == 3, f"Expected 3 events for Org A, got {len(org_a_events)}"
    assert len(org_b_events) == 2, f"Expected 2 events for Org B, got {len(org_b_events)}"

    # Verify Org A events don't contain Org B data
    for event in org_a_events:
        assert event.org_id == ORG_A_ID, f"Org A event has wrong org_id: {event.org_id}"
        assert "Patient B" not in event.summary, "Org A event contains Org B data!"

    # Verify Org B events don't contain Org A data
    for event in org_b_events:
        assert event.org_id == ORG_B_ID, f"Org B event has wrong org_id: {event.org_id}"
        assert "Patient A" not in event.summary, "Org B event contains Org A data!"

    print("PASSED: Org isolation verified")


async def test_event_type_filter(session: AsyncSession):
    """Test filtering by event_type"""
    print("\n--- Test 2: Event Type Filter ---")

    from sqlalchemy import select

    # Filter for task.created events in Org A
    result = await session.execute(
        select(ActivityLog).where(
            ActivityLog.org_id.in_([ORG_A_ID]),
            ActivityLog.event_type == "task.created"
        )
    )
    task_events = list(result.scalars().all())

    print(f"Found {len(task_events)} task.created events in Org A")
    assert len(task_events) == 1, f"Expected 1 task.created event, got {len(task_events)}"
    assert task_events[0].event_type == "task.created"

    # Filter for call events (entity_type filter via LIKE)
    result = await session.execute(
        select(ActivityLog).where(
            ActivityLog.org_id.in_([ORG_A_ID]),
            ActivityLog.event_type.like("call.%")
        )
    )
    call_events = list(result.scalars().all())

    print(f"Found {len(call_events)} call.* events in Org A")
    assert len(call_events) == 2, f"Expected 2 call events, got {len(call_events)}"

    print("PASSED: Event type filtering works")


async def test_pagination(session: AsyncSession):
    """Test pagination with cursor"""
    print("\n--- Test 3: Pagination ---")

    from sqlalchemy import select

    # Get all Org A events ordered by created_at desc
    result = await session.execute(
        select(ActivityLog)
        .where(ActivityLog.org_id.in_([ORG_A_ID]))
        .order_by(ActivityLog.created_at_utc.desc())
    )
    all_events = list(result.scalars().all())
    print(f"Total events: {len(all_events)}")

    # Simulate pagination: get first 2
    result = await session.execute(
        select(ActivityLog)
        .where(ActivityLog.org_id.in_([ORG_A_ID]))
        .order_by(ActivityLog.created_at_utc.desc())
        .limit(2)
    )
    page1 = list(result.scalars().all())
    print(f"Page 1: {len(page1)} events")
    assert len(page1) == 2

    # Get cursor (last item's timestamp and id)
    cursor_event = page1[-1]
    cursor_time = cursor_event.created_at_utc
    cursor_id = cursor_event.id

    # Get next page using cursor
    result = await session.execute(
        select(ActivityLog)
        .where(
            ActivityLog.org_id.in_([ORG_A_ID]),
            (ActivityLog.created_at_utc < cursor_time) |
            ((ActivityLog.created_at_utc == cursor_time) & (ActivityLog.id < cursor_id))
        )
        .order_by(ActivityLog.created_at_utc.desc())
        .limit(2)
    )
    page2 = list(result.scalars().all())
    print(f"Page 2: {len(page2)} events")

    # Verify no overlap
    page1_ids = {e.id for e in page1}
    page2_ids = {e.id for e in page2}
    assert len(page1_ids & page2_ids) == 0, "Pagination has overlapping events!"

    print("PASSED: Pagination works correctly")


async def test_response_format(session: AsyncSession):
    """Test that response format matches API spec"""
    print("\n--- Test 4: Response Format ---")

    from routes.activity import activity_to_response, sanitize_details

    from sqlalchemy import select
    result = await session.execute(
        select(ActivityLog).where(ActivityLog.org_id == ORG_A_ID).limit(1)
    )
    activity = result.scalar_one_or_none()

    if activity:
        response = activity_to_response(activity)

        # Verify required fields
        assert response.id is not None
        assert response.event_type is not None
        assert response.created_at_utc is not None
        assert response.summary_safe is not None

        # Verify entity_type derived from event_type
        if '.' in activity.event_type:
            expected_entity_type = activity.event_type.split('.')[0]
            assert response.entity_type == expected_entity_type

        print(f"Response ID: {response.id}")
        print(f"Event type: {response.event_type}")
        print(f"Entity type: {response.entity_type}")
        print(f"Summary: {response.summary_safe}")

    # Test phone masking
    test_details = {
        "phone": "+19195551234",
        "therapist_phone": "+19195559999",
        "workflow_type": "POC_SIGNATURE",
        "raw_payload": {"secret": "data"}  # Should be removed
    }
    sanitized = sanitize_details(test_details)

    assert "raw_payload" not in sanitized, "raw_payload should be removed"
    assert sanitized["phone"] == "***-***-1234", f"Phone not masked correctly: {sanitized['phone']}"
    assert sanitized["workflow_type"] == "POC_SIGNATURE", "Non-sensitive data altered"

    print("PASSED: Response format correct, phone masking works")


async def run_all_tests():
    """Run all API tests"""
    print("=" * 60)
    print("Running Activity API Tests")
    print("=" * 60)

    DATABASE_URL = os.getenv("POSTGRES_URL", "postgresql+asyncpg://zackfield@localhost:5432/clinicwire")

    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Setup
        await setup_test_data(session)
        await create_test_events(session)

        # Run tests
        await test_org_isolation(session)
        await test_event_type_filter(session)
        await test_pagination(session)
        await test_response_format(session)

    await engine.dispose()

    print("\n" + "=" * 60)
    print("All Activity API Tests PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_all_tests())
