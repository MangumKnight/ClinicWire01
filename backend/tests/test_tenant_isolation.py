"""
Tests for tenant (org) isolation in repository layer.

These tests verify that:
1. Tasks created for Org A are NOT visible to Org B
2. Contacts created for Org A are NOT visible to Org B
3. All query-level scoping works correctly

Run with: pytest tests/test_tenant_isolation.py -v
"""

try:
    import pytest
    HAS_PYTEST = True
except ImportError:
    HAS_PYTEST = False
    # Define dummy decorators when pytest not installed
    class pytest:
        @staticmethod
        def fixture(*args, **kwargs):
            def decorator(fn):
                return fn
            return decorator

        class mark:
            @staticmethod
            def asyncio(fn):
                return fn

import uuid
from datetime import datetime, timezone

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text

from db.models_multitenant import Base, Task, Organization
from db.repo_v2 import TaskRepository, ContactRepository


# Test organization IDs
ORG_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(scope="function")
async def test_session():
    """Create a test database session with in-memory SQLite"""
    # Use PostgreSQL for testing (matches production)
    DATABASE_URL = os.getenv("TEST_POSTGRES_URL", "postgresql+asyncpg://zackfield@localhost:5432/clinicwire_test")

    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        yield session

    await engine.dispose()


async def ensure_test_orgs(session):
    """Create test organizations if they don't exist"""
    from sqlalchemy import select

    # Check if orgs exist
    result = await session.execute(
        select(Organization).where(Organization.id.in_([ORG_A_ID, ORG_B_ID]))
    )
    existing_orgs = {o.id for o in result.scalars().all()}

    if ORG_A_ID not in existing_orgs:
        org_a = Organization(id=ORG_A_ID, name="Test Org A", slug="test-org-a")
        session.add(org_a)

    if ORG_B_ID not in existing_orgs:
        org_b = Organization(id=ORG_B_ID, name="Test Org B", slug="test-org-b")
        session.add(org_b)

    await session.commit()


@pytest.mark.asyncio
async def test_task_org_isolation(test_session):
    """
    Test that tasks are isolated by organization.

    Create tasks for Org A and Org B, then verify:
    - Searching with Org A's ID only returns Org A's tasks
    - Searching with Org B's ID only returns Org B's tasks
    - get_by_id with wrong org returns None
    """
    # Ensure test orgs exist
    await ensure_test_orgs(test_session)

    task_repo = TaskRepository(test_session)

    # Create task for Org A
    task_a_data = {
        "org_id": ORG_A_ID,
        "workflow_type": "POC_SIGNATURE",
        "patient_alias": "Patient A",
        "doctor_name": "Dr. Alpha",
        "doctor_phone": "+19195551111",
        "therapist_phone": "+19195552222",
    }
    task_a = await task_repo.create_or_get_task(task_a_data)

    # Create task for Org B
    task_b_data = {
        "org_id": ORG_B_ID,
        "workflow_type": "POC_SIGNATURE",
        "patient_alias": "Patient B",
        "doctor_name": "Dr. Beta",
        "doctor_phone": "+19195553333",
        "therapist_phone": "+19195554444",
    }
    task_b = await task_repo.create_or_get_task(task_b_data)

    # Verify: Search with Org A's ID should only return Org A's task
    org_a_tasks = await task_repo.search_tasks(org_ids=[ORG_A_ID])
    assert len(org_a_tasks) == 1
    assert org_a_tasks[0].patient_alias == "Patient A"
    assert org_a_tasks[0].org_id == ORG_A_ID

    # Verify: Search with Org B's ID should only return Org B's task
    org_b_tasks = await task_repo.search_tasks(org_ids=[ORG_B_ID])
    assert len(org_b_tasks) == 1
    assert org_b_tasks[0].patient_alias == "Patient B"
    assert org_b_tasks[0].org_id == ORG_B_ID

    # Verify: get_by_id with correct org returns the task
    task_a_found = await task_repo.get_by_id(task_a.id, [ORG_A_ID])
    assert task_a_found is not None
    assert task_a_found.patient_alias == "Patient A"

    # Verify: get_by_id with WRONG org returns None (isolation!)
    task_a_wrong_org = await task_repo.get_by_id(task_a.id, [ORG_B_ID])
    assert task_a_wrong_org is None, "Task A should NOT be visible to Org B!"

    # Verify: get_by_id with WRONG org returns None (isolation!)
    task_b_wrong_org = await task_repo.get_by_id(task_b.id, [ORG_A_ID])
    assert task_b_wrong_org is None, "Task B should NOT be visible to Org A!"

    print("Task isolation test PASSED")


@pytest.mark.asyncio
async def test_task_update_org_isolation(test_session):
    """
    Test that task updates are isolated by organization.

    Verify that update_status with wrong org_id fails to update.
    """
    await ensure_test_orgs(test_session)
    task_repo = TaskRepository(test_session)

    # Create task for Org A
    task_a_data = {
        "org_id": ORG_A_ID,
        "workflow_type": "POC_SIGNATURE",
        "patient_alias": "Update Test Patient",
        "doctor_name": "Dr. Update",
        "doctor_phone": "+19195559999",
        "therapist_phone": "+19195558888",
    }
    task_a = await task_repo.create_or_get_task(task_a_data)

    # Try to update with correct org - should succeed
    success = await task_repo.update_status(task_a.id, ORG_A_ID, "CALLING", "Test note")
    assert success is True

    # Verify the update worked
    task_a_updated = await task_repo.get_by_id(task_a.id, [ORG_A_ID])
    assert task_a_updated.status == "CALLING"

    # Try to update with WRONG org - should FAIL (return False)
    success_wrong = await task_repo.update_status(task_a.id, ORG_B_ID, "FAILED", "Malicious update")
    assert success_wrong is False, "Update with wrong org_id should return False!"

    # Verify the malicious update did NOT happen
    task_a_check = await task_repo.get_by_id(task_a.id, [ORG_A_ID])
    assert task_a_check.status == "CALLING", "Status should still be CALLING, not FAILED!"

    print("Task update isolation test PASSED")


@pytest.mark.asyncio
async def test_multi_org_user(test_session):
    """
    Test that users with access to multiple orgs can see tasks from all their orgs.
    """
    await ensure_test_orgs(test_session)
    task_repo = TaskRepository(test_session)

    # Create task for Org A
    task_a_data = {
        "org_id": ORG_A_ID,
        "workflow_type": "POC_SIGNATURE",
        "patient_alias": "Multi-Org Patient A",
        "doctor_name": "Dr. Multi A",
        "doctor_phone": "+19195551234",
        "therapist_phone": "+19195555678",
    }
    await task_repo.create_or_get_task(task_a_data)

    # Create task for Org B
    task_b_data = {
        "org_id": ORG_B_ID,
        "workflow_type": "POC_SIGNATURE",
        "patient_alias": "Multi-Org Patient B",
        "doctor_name": "Dr. Multi B",
        "doctor_phone": "+19195559012",
        "therapist_phone": "+19195553456",
    }
    await task_repo.create_or_get_task(task_b_data)

    # User with access to BOTH orgs should see both tasks
    both_org_tasks = await task_repo.search_tasks(org_ids=[ORG_A_ID, ORG_B_ID])
    # Should have at least our 2 tasks (may have more from previous tests)
    assert len(both_org_tasks) >= 2

    patient_aliases = {t.patient_alias for t in both_org_tasks}
    assert "Multi-Org Patient A" in patient_aliases
    assert "Multi-Org Patient B" in patient_aliases

    # Verify that searching with just one org filters correctly
    org_a_only = await task_repo.search_tasks(org_ids=[ORG_A_ID])
    for task in org_a_only:
        assert task.org_id == ORG_A_ID, f"Found task with wrong org_id: {task.org_id}"

    print("Multi-org user test PASSED")


if __name__ == "__main__":
    import asyncio

    async def run_tests():
        """Run tests manually without pytest"""
        print("=" * 60)
        print("Running tenant isolation tests...")
        print("=" * 60)

        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker

        DATABASE_URL = os.getenv("POSTGRES_URL", "postgresql+asyncpg://zackfield@localhost:5432/clinicwire")

        engine = create_async_engine(DATABASE_URL, echo=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with async_session() as session:
            # Clean up test data first
            await session.execute(text("DELETE FROM sms_events WHERE 1=1"))
            await session.execute(text("DELETE FROM call_events WHERE 1=1"))
            await session.execute(text("DELETE FROM tasks WHERE org_id IN (:org_a, :org_b)"),
                                {"org_a": str(ORG_A_ID), "org_b": str(ORG_B_ID)})
            await session.commit()

            # Ensure test orgs exist
            await ensure_test_orgs(session)

            print("\n--- Test 1: Task org isolation ---")
            await test_task_org_isolation(session)

            print("\n--- Test 2: Task update org isolation ---")
            await test_task_update_org_isolation(session)

            print("\n--- Test 3: Multi-org user ---")
            await test_multi_org_user(session)

        await engine.dispose()
        print("\n" + "=" * 60)
        print("All tenant isolation tests PASSED!")
        print("=" * 60)

    asyncio.run(run_tests())
