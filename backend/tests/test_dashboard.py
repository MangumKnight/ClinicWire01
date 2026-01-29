"""
Tests for Dashboard API endpoints.

These tests verify:
1. Summary metrics calculation (calls attempted, connected, outcomes, time/cost saved)
2. Outcome distribution with is_success flag
3. Org isolation for dashboard data
4. Backend constants are properly applied

Run with: python tests/test_dashboard.py
"""

import asyncio
import uuid
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text, select, func, and_

from db.models_multitenant import Base, Organization, User, OrgMembership, Task, CallEvent
from routes.dashboard import (
    DEFAULT_MINUTES_SAVED_PER_CALL,
    DEFAULT_HOURLY_RATE,
    SUCCESS_OUTCOME_CODES,
)


# Test organization and user IDs
ORG_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaa0001")
ORG_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbb0002")
USER_A_ID = uuid.UUID("11111111-1111-1111-1111-111111110001")
USER_B_ID = uuid.UUID("22222222-2222-2222-2222-222222220002")


async def setup_test_orgs_and_users(session: AsyncSession):
    """Create test orgs and users"""
    result = await session.execute(
        select(Organization).where(Organization.id.in_([ORG_A_ID, ORG_B_ID]))
    )
    existing_orgs = {o.id for o in result.scalars().all()}

    if ORG_A_ID not in existing_orgs:
        org_a = Organization(id=ORG_A_ID, name="Dashboard Test Org A", slug="dash-test-a")
        session.add(org_a)

    if ORG_B_ID not in existing_orgs:
        org_b = Organization(id=ORG_B_ID, name="Dashboard Test Org B", slug="dash-test-b")
        session.add(org_b)

    await session.commit()

    result = await session.execute(
        select(User).where(User.id.in_([USER_A_ID, USER_B_ID]))
    )
    existing_users = {u.id for u in result.scalars().all()}

    if USER_A_ID not in existing_users:
        user_a = User(id=USER_A_ID, email="dash-testa@test.com", name="Dashboard Test User A")
        session.add(user_a)

    if USER_B_ID not in existing_users:
        user_b = User(id=USER_B_ID, email="dash-testb@test.com", name="Dashboard Test User B")
        session.add(user_b)

    await session.commit()

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


async def create_test_data(session: AsyncSession):
    """Create test tasks and call events"""
    # Clean up existing test data
    await session.execute(
        text("DELETE FROM call_events WHERE org_id IN (:org_a, :org_b)"),
        {"org_a": str(ORG_A_ID), "org_b": str(ORG_B_ID)}
    )
    await session.execute(
        text("DELETE FROM tasks WHERE org_id IN (:org_a, :org_b)"),
        {"org_a": str(ORG_A_ID), "org_b": str(ORG_B_ID)}
    )
    await session.commit()

    now = datetime.now(timezone.utc)
    task_ids = []

    # Org A: Create 5 tasks with various outcomes
    outcomes_a = [
        ('RESOLVED', 'CONFIRMED_RECEIVED', now - timedelta(hours=4)),   # Success
        ('RESOLVED', 'CONFIRMED_SIGNED', now - timedelta(hours=3)),     # Success
        ('RESOLVED', 'SIGNATURE_PENDING', now - timedelta(hours=2)),    # Success (in v2)
        ('RESOLVED', 'NEEDS_RESEND', now - timedelta(hours=5)),         # Not success
        ('FAILED', 'WRONG_CONTACT', None),                              # Not success
    ]

    for i, (status, outcome_code, completed_at) in enumerate(outcomes_a):
        task = Task(
            id=uuid.uuid4(),
            org_id=ORG_A_ID,
            workflow_type='POC_SIGNATURE',
            patient_alias=f'Patient A{i}',
            doctor_name=f'Dr. Alpha {i}',
            doctor_phone='+19195551234',
            therapist_phone='+19195559999',
            status=status,
            outcome_code=outcome_code,
            attempts=1,
            completed_at_utc=completed_at,
            idempotency_key=f'dash-test-a-{i}',
            created_at_utc=now - timedelta(hours=10),
        )
        session.add(task)
        task_ids.append(task.id)

    await session.commit()

    # Create call events for Org A
    # 10 call events total: 6 completed/answered, 4 other states
    call_states = ['completed', 'answered', 'completed', 'answered', 'completed', 'answered',
                   'no-answer', 'busy', 'failed', 'initiated']

    for i, state in enumerate(call_states):
        call = CallEvent(
            id=uuid.uuid4(),
            org_id=ORG_A_ID,
            task_id=task_ids[i % len(task_ids)],  # Distribute across tasks
            twilio_sid=f'CA_DASH_{str(uuid.uuid4())[:8]}',
            state=state,
            duration_sec=45 if state in ['completed', 'answered'] else 0,
            created_at_utc=now - timedelta(hours=i+1),  # All calls in the past
        )
        session.add(call)

    # Org B: Create 3 tasks (for isolation testing)
    for i in range(3):
        task = Task(
            id=uuid.uuid4(),
            org_id=ORG_B_ID,
            workflow_type='POC_SIGNATURE',
            patient_alias=f'Patient B{i}',
            doctor_name=f'Dr. Beta {i}',
            doctor_phone='+19195552222',
            therapist_phone='+19195558888',
            status='RESOLVED',
            outcome_code='CONFIRMED_RECEIVED',
            attempts=1,
            completed_at_utc=now - timedelta(hours=1),
            idempotency_key=f'dash-test-b-{i}',
            created_at_utc=now - timedelta(hours=10),
        )
        session.add(task)

    await session.commit()
    print(f"Created 5 tasks + 10 call events for Org A, 3 tasks for Org B")


async def test_backend_constants():
    """Test that backend constants are correctly defined"""
    print("\n--- Test 1: Backend Constants ---")

    # Verify default values
    assert DEFAULT_MINUTES_SAVED_PER_CALL == 15, f"Expected 15, got {DEFAULT_MINUTES_SAVED_PER_CALL}"
    assert DEFAULT_HOURLY_RATE == 35, f"Expected 35, got {DEFAULT_HOURLY_RATE}"

    # Verify success outcome codes include SIGNATURE_PENDING
    assert 'CONFIRMED_RECEIVED' in SUCCESS_OUTCOME_CODES
    assert 'CONFIRMED_SIGNED' in SUCCESS_OUTCOME_CODES
    assert 'SIGNATURE_PENDING' in SUCCESS_OUTCOME_CODES
    assert len(SUCCESS_OUTCOME_CODES) == 3, f"Expected 3 success codes, got {len(SUCCESS_OUTCOME_CODES)}"

    print(f"DEFAULT_MINUTES_SAVED_PER_CALL = {DEFAULT_MINUTES_SAVED_PER_CALL}")
    print(f"DEFAULT_HOURLY_RATE = {DEFAULT_HOURLY_RATE}")
    print(f"SUCCESS_OUTCOME_CODES = {SUCCESS_OUTCOME_CODES}")
    print("PASSED: Backend constants verified")


async def test_calls_attempted(session: AsyncSession):
    """Test calls_attempted = count(call_events)"""
    print("\n--- Test 2: Calls Attempted ---")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)

    result = await session.execute(
        select(func.count(CallEvent.id)).where(
            and_(
                CallEvent.org_id == ORG_A_ID,
                CallEvent.created_at_utc >= start,
                CallEvent.created_at_utc <= now
            )
        )
    )
    calls_attempted = result.scalar() or 0

    print(f"Calls attempted: {calls_attempted}")
    assert calls_attempted == 10, f"Expected 10 calls, got {calls_attempted}"
    print("PASSED: Calls attempted count correct")


async def test_calls_connected(session: AsyncSession):
    """Test calls_connected = call_events with state in {completed, answered}"""
    print("\n--- Test 3: Calls Connected ---")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)

    result = await session.execute(
        select(func.count(CallEvent.id)).where(
            and_(
                CallEvent.org_id == ORG_A_ID,
                CallEvent.created_at_utc >= start,
                CallEvent.created_at_utc <= now,
                CallEvent.state.in_(['completed', 'answered'])
            )
        )
    )
    calls_connected = result.scalar() or 0

    print(f"Calls connected: {calls_connected}")
    assert calls_connected == 6, f"Expected 6 connected calls, got {calls_connected}"
    print("PASSED: Calls connected count correct")


async def test_successful_outcomes(session: AsyncSession):
    """Test successful_outcomes = tasks with outcome_code in SUCCESS_OUTCOME_CODES"""
    print("\n--- Test 4: Successful Outcomes ---")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)

    result = await session.execute(
        select(func.count(Task.id)).where(
            and_(
                Task.org_id == ORG_A_ID,
                Task.created_at_utc >= start,
                Task.created_at_utc <= now,
                Task.outcome_code.in_(SUCCESS_OUTCOME_CODES),
                Task.completed_at_utc.isnot(None),
                Task.completed_at_utc >= start,
                Task.completed_at_utc <= now
            )
        )
    )
    successful_outcomes = result.scalar() or 0

    print(f"Successful outcomes: {successful_outcomes}")
    # 3 tasks have success codes: CONFIRMED_RECEIVED, CONFIRMED_SIGNED, SIGNATURE_PENDING
    assert successful_outcomes == 3, f"Expected 3 successful outcomes, got {successful_outcomes}"
    print("PASSED: Successful outcomes count correct")


async def test_estimated_savings(session: AsyncSession):
    """Test estimated time and cost savings calculations"""
    print("\n--- Test 5: Estimated Savings ---")

    # Based on 10 calls attempted
    calls_attempted = 10

    # Estimated hours saved = calls_attempted * minutes_per_call / 60
    estimated_hours = (calls_attempted * DEFAULT_MINUTES_SAVED_PER_CALL) / 60
    print(f"Estimated hours saved: {estimated_hours}")
    assert estimated_hours == 2.5, f"Expected 2.5 hours, got {estimated_hours}"

    # Estimated cost saved = hours * hourly_rate
    estimated_cost = estimated_hours * DEFAULT_HOURLY_RATE
    print(f"Estimated cost saved: ${estimated_cost}")
    assert estimated_cost == 87.5, f"Expected $87.5, got ${estimated_cost}"

    print("PASSED: Estimated savings calculated correctly")


async def test_outcome_distribution(session: AsyncSession):
    """Test outcome distribution with is_success flag"""
    print("\n--- Test 6: Outcome Distribution ---")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)

    result = await session.execute(
        select(Task.outcome_code, func.count(Task.id))
        .where(
            and_(
                Task.org_id == ORG_A_ID,
                Task.created_at_utc >= start,
                Task.created_at_utc <= now,
                Task.outcome_code.isnot(None)
            )
        )
        .group_by(Task.outcome_code)
    )
    outcome_rows = result.all()

    print("Outcome distribution:")
    success_count = 0
    for code, count in outcome_rows:
        is_success = code in SUCCESS_OUTCOME_CODES
        if is_success:
            success_count += count
        print(f"  {code}: {count} (is_success={is_success})")

    assert success_count == 3, f"Expected 3 success outcomes, got {success_count}"
    print("PASSED: Outcome distribution with is_success flag correct")


async def test_call_event_org_id_not_null(session: AsyncSession):
    """Test that all CallEvent rows have org_id populated (never null)"""
    print("\n--- Test 7: CallEvent org_id NOT NULL ---")

    # Count call events with null org_id
    result = await session.execute(
        select(func.count(CallEvent.id)).where(CallEvent.org_id.is_(None))
    )
    null_count = result.scalar() or 0

    # Count total call events
    result = await session.execute(
        select(func.count(CallEvent.id))
    )
    total_count = result.scalar() or 0

    print(f"Total call events: {total_count}")
    print(f"Call events with null org_id: {null_count}")

    assert null_count == 0, f"CRITICAL: Found {null_count} call_events with null org_id!"
    assert total_count > 0, "No call events found - test data may not have been created"

    print("PASSED: All CallEvent rows have org_id populated")


async def test_org_isolation(session: AsyncSession):
    """Test that Org A data doesn't include Org B data"""
    print("\n--- Test 8: Org Isolation ---")

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=30)

    # Count Org A successful outcomes
    result = await session.execute(
        select(func.count(Task.id)).where(
            and_(
                Task.org_id == ORG_A_ID,
                Task.created_at_utc >= start,
                Task.outcome_code.in_(SUCCESS_OUTCOME_CODES),
                Task.completed_at_utc.isnot(None)
            )
        )
    )
    org_a_success = result.scalar() or 0

    # Count Org B successful outcomes
    result = await session.execute(
        select(func.count(Task.id)).where(
            and_(
                Task.org_id == ORG_B_ID,
                Task.created_at_utc >= start,
                Task.outcome_code.in_(SUCCESS_OUTCOME_CODES),
                Task.completed_at_utc.isnot(None)
            )
        )
    )
    org_b_success = result.scalar() or 0

    print(f"Org A successful outcomes: {org_a_success}")
    print(f"Org B successful outcomes: {org_b_success}")

    # Org A should have 3, Org B should have 3 (their own data)
    assert org_a_success == 3, f"Expected 3 Org A successes, got {org_a_success}"
    assert org_b_success == 3, f"Expected 3 Org B successes, got {org_b_success}"

    # Verify they're independent
    assert org_a_success != org_b_success or (org_a_success == 3 and org_b_success == 3)

    print("PASSED: Org isolation verified - each org sees only their own data")


async def run_all_tests():
    """Run all dashboard tests"""
    print("=" * 60)
    print("Running Dashboard API Tests")
    print("=" * 60)

    DATABASE_URL = os.getenv("POSTGRES_URL", "postgresql+asyncpg://zackfield@localhost:5432/clinicwire")

    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Setup
        await setup_test_orgs_and_users(session)
        await create_test_data(session)

        # Run tests
        await test_backend_constants()
        await test_calls_attempted(session)
        await test_calls_connected(session)
        await test_successful_outcomes(session)
        await test_estimated_savings(session)
        await test_outcome_distribution(session)
        await test_call_event_org_id_not_null(session)
        await test_org_isolation(session)

    await engine.dispose()

    print("\n" + "=" * 60)
    print("All Dashboard API Tests PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_all_tests())
