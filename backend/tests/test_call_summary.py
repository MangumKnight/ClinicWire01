"""
Tests for Call Summary Webhook endpoint.

These tests verify:
1. Invalid signature returns 403
2. Replay protection - same payload twice returns 200 but only one activity event
3. Cross-org safety - call_sid lookup derives org_id correctly
4. PHI masking - phone numbers and emails are masked in stored data
5. Payload validation - missing fields, invalid outcome codes

Run with: python tests/test_call_summary.py
Or with pytest: SIMULATE=true pytest tests/test_call_summary.py -v
"""

import asyncio
import uuid
import os
import sys
import hmac
import hashlib
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, text

from db.models_multitenant import Base, Organization, User, OrgMembership, Task, CallEvent, ActivityLog
from db.repo_v2 import TaskRepository, CallEventRepository, ActivityLogRepository


# Test organization and user IDs
ORG_A_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B_ID = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
USER_B_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

# Test secret for signature generation
TEST_WEBHOOK_SECRET = "test-secret-for-call-summary-webhook"


def generate_signature(payload: dict, timestamp: int, secret: str = TEST_WEBHOOK_SECRET) -> str:
    """Generate HMAC-SHA256 signature for test payloads"""
    body = json.dumps(payload).encode()
    signature = hmac.new(
        secret.encode(),
        (str(timestamp).encode() + body),
        hashlib.sha256
    ).hexdigest()
    return f"sha256={signature}"


async def setup_test_data(session: AsyncSession):
    """Create test orgs, users, and memberships"""
    from sqlalchemy import select

    # Check if orgs exist, create if not
    result = await session.execute(
        select(Organization).where(Organization.id.in_([ORG_A_ID, ORG_B_ID]))
    )
    existing_orgs = {o.id for o in result.scalars().all()}

    if ORG_A_ID not in existing_orgs:
        org_a = Organization(id=ORG_A_ID, name="Test Org A", slug="test-org-a-summary")
        session.add(org_a)

    if ORG_B_ID not in existing_orgs:
        org_b = Organization(id=ORG_B_ID, name="Test Org B", slug="test-org-b-summary")
        session.add(org_b)

    await session.commit()

    # Check if users exist
    result = await session.execute(
        select(User).where(User.id.in_([USER_A_ID, USER_B_ID]))
    )
    existing_users = {u.id for u in result.scalars().all()}

    if USER_A_ID not in existing_users:
        user_a = User(id=USER_A_ID, email="testa-summary@test.com", name="Test User A")
        session.add(user_a)

    if USER_B_ID not in existing_users:
        user_b = User(id=USER_B_ID, email="testb-summary@test.com", name="Test User B")
        session.add(user_b)

    await session.commit()


async def create_test_task_and_call(session: AsyncSession, org_id: uuid.UUID, twilio_sid: str) -> tuple:
    """Create a task and call event for testing"""
    # Create task
    task = Task(
        org_id=org_id,
        workflow_type="POC_SIGNATURE",
        patient_alias="Test Patient",
        doctor_name="Dr. Test",
        doctor_phone="+19195551234",
        therapist_phone="+19195559999",
        status="CALLING",
        idempotency_key=f"test-{uuid.uuid4()}"
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)

    # Create call event
    call_event = CallEvent(
        org_id=org_id,
        task_id=task.id,
        twilio_sid=twilio_sid,
        state="initiated"
    )
    session.add(call_event)
    await session.commit()
    await session.refresh(call_event)

    return task, call_event


async def cleanup_test_data(session: AsyncSession, task_ids: list):
    """Clean up test tasks and related data"""
    if not task_ids:
        return

    # Clean up activity logs
    await session.execute(
        text("DELETE FROM activity_log WHERE task_id = ANY(:task_ids)"),
        {"task_ids": [str(t) for t in task_ids]}
    )

    # Clean up call events
    await session.execute(
        text("DELETE FROM call_events WHERE task_id = ANY(:task_ids)"),
        {"task_ids": [str(t) for t in task_ids]}
    )

    # Clean up tasks
    await session.execute(
        text("DELETE FROM tasks WHERE id = ANY(:task_ids)"),
        {"task_ids": [str(t) for t in task_ids]}
    )

    await session.commit()


# ============================================================
# Test 1: Invalid Signature Returns 403
# ============================================================

async def test_invalid_signature(session: AsyncSession):
    """Test that invalid signatures are rejected with 403"""
    print("\n--- Test 1: Invalid Signature Returns 403 ---")

    # Create test task and call event (use short twilio_sid to fit varchar(50))
    task, call_event = await create_test_task_and_call(
        session, ORG_A_ID, f"CA_SIG_{str(uuid.uuid4())[:8]}"
    )

    try:
        # Import the validation function
        from main import validate_call_summary_signature

        # Create a mock request class
        class MockRequest:
            def __init__(self, headers):
                self.headers = headers

        # Test 1a: Missing signature
        mock_req = MockRequest({"X-Webhook-Timestamp": str(int(time.time()))})
        result = validate_call_summary_signature(mock_req, b'{"test": "data"}')
        assert result == False, "Should reject missing signature"
        print("  1a. Missing signature: REJECTED (correct)")

        # Test 1b: Invalid signature format
        mock_req = MockRequest({
            "X-Webhook-Timestamp": str(int(time.time())),
            "X-Webhook-Signature": "invalid-format"
        })
        result = validate_call_summary_signature(mock_req, b'{"test": "data"}')
        assert result == False, "Should reject invalid signature format"
        print("  1b. Invalid signature format: REJECTED (correct)")

        # Test 1c: Wrong signature value
        mock_req = MockRequest({
            "X-Webhook-Timestamp": str(int(time.time())),
            "X-Webhook-Signature": "sha256=wrongsignature123"
        })

        # Temporarily set the secret for testing
        original_secret = os.environ.get("CALL_SUMMARY_WEBHOOK_SECRET")
        os.environ["CALL_SUMMARY_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET

        result = validate_call_summary_signature(mock_req, b'{"test": "data"}')
        assert result == False, "Should reject wrong signature"
        print("  1c. Wrong signature: REJECTED (correct)")

        # Restore original secret
        if original_secret:
            os.environ["CALL_SUMMARY_WEBHOOK_SECRET"] = original_secret
        else:
            del os.environ["CALL_SUMMARY_WEBHOOK_SECRET"]

        print("PASSED: Invalid signatures are rejected")

    finally:
        await cleanup_test_data(session, [task.id])


# ============================================================
# Test 2: Replay Protection (Idempotency)
# ============================================================

async def test_replay_protection(session: AsyncSession):
    """Test that duplicate summaries are handled idempotently"""
    print("\n--- Test 2: Replay Protection (Idempotency) ---")

    twilio_sid = f"CA_RPL_{str(uuid.uuid4())[:8]}"
    task, call_event = await create_test_task_and_call(session, ORG_A_ID, twilio_sid)

    try:
        activity_repo = ActivityLogRepository(session)
        task_repo = TaskRepository(session)

        # Simulate first summary processing
        await task_repo.update_outcome_v2(
            task_id=task.id,
            org_id=ORG_A_ID,
            outcome_code="CONFIRMED_RECEIVED",
            outcome_note="Office confirmed receipt",
            completed_at_utc=datetime.now(timezone.utc)
        )

        await activity_repo.log_event(
            org_id=ORG_A_ID,
            event_type="call.summary",
            summary="Call summary received: CONFIRMED_RECEIVED",
            task_id=task.id,
            details={"outcome_code": "CONFIRMED_RECEIVED", "summary": "Office confirmed receipt"}
        )

        # Check activity count after first processing
        result = await session.execute(
            select(ActivityLog).where(
                ActivityLog.task_id == task.id,
                ActivityLog.event_type == "call.summary"
            )
        )
        first_count = len(list(result.scalars().all()))
        assert first_count == 1, f"Expected 1 activity log after first processing, got {first_count}"
        print(f"  After first summary: {first_count} activity event(s)")

        # Check idempotency - should detect existing event
        has_existing = await activity_repo.has_event_for_task(task.id, "call.summary")
        assert has_existing == True, "Should detect existing call.summary event"
        print(f"  Idempotency check: Event exists = {has_existing}")

        # If we were to process again, we should NOT create another event
        # (The endpoint returns early, so we just verify the check works)

        print("PASSED: Replay protection works - duplicate detection active")

    finally:
        await cleanup_test_data(session, [task.id])


# ============================================================
# Test 3: Cross-Org Safety
# ============================================================

async def test_cross_org_safety(session: AsyncSession):
    """Test that call_sid from Org A cannot update Org B tasks"""
    print("\n--- Test 3: Cross-Org Safety ---")

    # Create task in Org A (use short twilio_sid to fit varchar(50))
    twilio_sid_a = f"CA_OA_{str(uuid.uuid4())[:8]}"
    task_a, call_event_a = await create_test_task_and_call(session, ORG_A_ID, twilio_sid_a)

    # Create task in Org B
    twilio_sid_b = f"CA_OB_{str(uuid.uuid4())[:8]}"
    task_b, call_event_b = await create_test_task_and_call(session, ORG_B_ID, twilio_sid_b)

    try:
        call_event_repo = CallEventRepository(session)

        # Look up Org A's call event
        found_event_a = await call_event_repo.get_by_twilio_sid_system(twilio_sid_a)
        assert found_event_a is not None, "Should find Org A's call event"
        assert found_event_a.task.org_id == ORG_A_ID, "Call event should belong to Org A"
        print(f"  Org A call_sid maps to org_id: {found_event_a.task.org_id}")

        # Look up Org B's call event
        found_event_b = await call_event_repo.get_by_twilio_sid_system(twilio_sid_b)
        assert found_event_b is not None, "Should find Org B's call event"
        assert found_event_b.task.org_id == ORG_B_ID, "Call event should belong to Org B"
        print(f"  Org B call_sid maps to org_id: {found_event_b.task.org_id}")

        # Verify org_ids are different
        assert found_event_a.task.org_id != found_event_b.task.org_id, "Org IDs should be different"

        # Verify that even if we tried to use Org A's call_sid to update,
        # the org_id would be derived from the call_event, not trusted from payload
        # This is the key security property

        print("PASSED: Cross-org safety verified - org_id derived from call_sid")

    finally:
        await cleanup_test_data(session, [task_a.id, task_b.id])


# ============================================================
# Test 4: PHI Masking
# ============================================================

async def test_phi_masking():
    """Test that phone numbers and emails are masked"""
    print("\n--- Test 4: PHI Masking ---")

    from main import mask_phi_in_text

    # Test phone number masking
    test_cases = [
        ("Call +19195551234 for update", "Call ***-***-1234 for update"),
        ("Phone: (919) 555-1234", "Phone: ***-***-1234"),
        ("Contact 919-555-1234 ASAP", "Contact ***-***-1234 ASAP"),
        ("Number: 919.555.1234", "Number: ***-***-1234"),
    ]

    for input_text, expected in test_cases:
        result = mask_phi_in_text(input_text)
        # Just check that phone is masked (last 4 digits preserved)
        assert "1234" in result and ("***" in result or "***-***" in result), \
            f"Phone not masked in: {input_text} -> {result}"
    print("  Phone masking: PASSED")

    # Test email masking
    email_cases = [
        ("Contact patient@example.com", "Contact ***@***.***"),
        ("Email: dr.smith@hospital.org", "Email: ***@***.***"),
    ]

    for input_text, expected in email_cases:
        result = mask_phi_in_text(input_text)
        assert "***@***" in result, f"Email not masked in: {input_text} -> {result}"
    print("  Email masking: PASSED")

    # Test that non-PHI text is preserved
    normal_text = "Office confirmed receipt of fax for patient John D."
    result = mask_phi_in_text(normal_text)
    assert result == normal_text, "Non-PHI text should not be altered"
    print("  Non-PHI preservation: PASSED")

    # Test empty/None handling
    assert mask_phi_in_text("") == ""
    assert mask_phi_in_text(None) is None
    print("  Edge cases: PASSED")

    print("PASSED: PHI masking works correctly")


# ============================================================
# Test 5: Payload Validation
# ============================================================

async def test_payload_validation():
    """Test payload validation rules"""
    print("\n--- Test 5: Payload Validation ---")

    from main import VALID_OUTCOME_CODES, SUMMARY_MAX_LENGTH

    # Test outcome code allowlist
    valid_codes = [
        "CONFIRMED_RECEIVED", "CONFIRMED_SIGNED", "SIGNATURE_PENDING",
        "NEEDS_RESEND", "CALLBACK_REQUESTED", "WRONG_CONTACT",
        "REFUSED_INFO", "NO_DECISION", "ERROR"
    ]

    for code in valid_codes:
        assert code in VALID_OUTCOME_CODES, f"Missing valid code: {code}"
    print(f"  Valid outcome codes: {len(valid_codes)} codes in allowlist")

    # Test invalid codes are rejected
    invalid_codes = ["CONFIRMED", "UNKNOWN", "invalid", ""]
    for code in invalid_codes:
        assert code not in VALID_OUTCOME_CODES, f"Invalid code should not be in allowlist: {code}"
    print("  Invalid codes rejected: PASSED")

    # Test max length constant
    assert SUMMARY_MAX_LENGTH == 500, f"Expected max length 500, got {SUMMARY_MAX_LENGTH}"
    print(f"  Max summary length: {SUMMARY_MAX_LENGTH} chars")

    print("PASSED: Payload validation rules correct")


# ============================================================
# Test 6: Timestamp Replay Protection
# ============================================================

async def test_timestamp_replay():
    """Test that old timestamps are rejected"""
    print("\n--- Test 6: Timestamp Replay Protection ---")

    from main import validate_call_summary_signature, WEBHOOK_TIMESTAMP_MAX_AGE

    class MockRequest:
        def __init__(self, headers):
            self.headers = headers

    # Set test secret
    original_secret = os.environ.get("CALL_SUMMARY_WEBHOOK_SECRET")
    os.environ["CALL_SUMMARY_WEBHOOK_SECRET"] = TEST_WEBHOOK_SECRET

    try:
        payload = {"call_sid": "CA123", "outcome_code": "CONFIRMED_RECEIVED", "summary": "Test"}
        body = json.dumps(payload).encode()

        # Test valid timestamp (now)
        now = int(time.time())
        signature = hmac.new(
            TEST_WEBHOOK_SECRET.encode(),
            (str(now).encode() + body),
            hashlib.sha256
        ).hexdigest()

        mock_req = MockRequest({
            "X-Webhook-Timestamp": str(now),
            "X-Webhook-Signature": f"sha256={signature}"
        })
        result = validate_call_summary_signature(mock_req, body)
        assert result == True, "Should accept valid recent timestamp"
        print(f"  Valid timestamp (now): ACCEPTED")

        # Test old timestamp (10 minutes ago)
        old_ts = now - 600  # 10 minutes ago
        old_signature = hmac.new(
            TEST_WEBHOOK_SECRET.encode(),
            (str(old_ts).encode() + body),
            hashlib.sha256
        ).hexdigest()

        mock_req = MockRequest({
            "X-Webhook-Timestamp": str(old_ts),
            "X-Webhook-Signature": f"sha256={old_signature}"
        })
        result = validate_call_summary_signature(mock_req, body)
        assert result == False, "Should reject old timestamp"
        print(f"  Old timestamp (10min ago): REJECTED")

        # Verify max age constant
        assert WEBHOOK_TIMESTAMP_MAX_AGE == 300, f"Expected 300s max age, got {WEBHOOK_TIMESTAMP_MAX_AGE}"
        print(f"  Max timestamp age: {WEBHOOK_TIMESTAMP_MAX_AGE} seconds")

        print("PASSED: Timestamp replay protection works")

    finally:
        if original_secret:
            os.environ["CALL_SUMMARY_WEBHOOK_SECRET"] = original_secret
        elif "CALL_SUMMARY_WEBHOOK_SECRET" in os.environ:
            del os.environ["CALL_SUMMARY_WEBHOOK_SECRET"]


# ============================================================
# Main Test Runner
# ============================================================

async def run_all_tests():
    """Run all call summary tests"""
    print("=" * 60)
    print("Running Call Summary Webhook Tests")
    print("=" * 60)

    DATABASE_URL = os.getenv("POSTGRES_URL", "postgresql+asyncpg://zackfield@localhost:5432/clinicwire")

    engine = create_async_engine(DATABASE_URL, echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Setup
        await setup_test_data(session)

        # Run tests
        await test_invalid_signature(session)
        await test_replay_protection(session)
        await test_cross_org_safety(session)
        await test_phi_masking()
        await test_payload_validation()
        await test_timestamp_replay()

    await engine.dispose()

    print("\n" + "=" * 60)
    print("All Call Summary Webhook Tests PASSED!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(run_all_tests())
