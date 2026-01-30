#!/usr/bin/env python3
"""
Seed demo data for ClinicWire.

Creates realistic tasks, call events, and activity logs for testing
the Tasks, Activity, and Impact tabs.

Safety:
- Refuses to run unless APP_ENV=development OR SEED_ENABLE=true
- Idempotent: uses deterministic IDs based on SEED_PREFIX, deletes and recreates

Usage:
  python scripts/seed_demo_data.py --org <ORG_ID> --count 30 --days 45
  python scripts/seed_demo_data.py --org demo  # Uses demo org by slug
"""

import argparse
import asyncio
import hashlib
import os
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, delete, text

from db.models_multitenant import Base, Organization, Task, CallEvent, ActivityLog


# =============================================================================
# CONFIGURATION
# =============================================================================

SEED_PREFIX = "SEED_"  # Prefix for idempotency keys to identify seeded data

# Workflow types
WORKFLOW_TYPES = [
    'POC_SIGNATURE',
    'REFERRAL_FOLLOWUP',
    'PRESCRIPTION_RENEWAL',
    'LAB_RESULTS',
    'APPOINTMENT_CONFIRMATION'
]

# Doctor names (realistic mix)
DOCTOR_NAMES = [
    "Dr. Sarah Chen",
    "Dr. Michael Rodriguez",
    "Dr. Emily Watson",
    "Dr. James Thompson",
    "Dr. Lisa Park",
    "Dr. Robert Martinez",
    "Dr. Jennifer Lee",
    "Dr. David Kim",
    "Dr. Amanda Foster",
    "Dr. Christopher Wright"
]

# Patient aliases (HIPAA-safe fake names)
PATIENT_ALIASES = [
    "Patient A. Smith", "Patient B. Johnson", "Patient C. Williams",
    "Patient D. Brown", "Patient E. Jones", "Patient F. Garcia",
    "Patient G. Miller", "Patient H. Davis", "Patient I. Martinez",
    "Patient J. Anderson", "Patient K. Taylor", "Patient L. Thomas",
    "Patient M. Jackson", "Patient N. White", "Patient O. Harris",
    "Patient P. Martin", "Patient Q. Thompson", "Patient R. Moore",
    "Patient S. Allen", "Patient T. Young", "Patient U. King",
    "Patient V. Wright", "Patient W. Lopez", "Patient X. Hill",
    "Patient Y. Scott", "Patient Z. Green", "Patient AA. Adams",
    "Patient BB. Baker", "Patient CC. Clark", "Patient DD. Lewis"
]

# Fake phone numbers (555 prefix = known fake)
DOCTOR_PHONES = [
    "+15551234001", "+15551234002", "+15551234003", "+15551234004",
    "+15551234005", "+15551234006", "+15551234007", "+15551234008",
    "+15551234009", "+15551234010"
]

THERAPIST_PHONES = [
    "+15559876001", "+15559876002", "+15559876003", "+15559876004",
    "+15559876005"
]

# Outcome distribution for completed tasks
OUTCOME_DISTRIBUTION = [
    ('CONFIRMED_RECEIVED', 0.35),     # 35% - confirmed receipt
    ('CONFIRMED_SIGNED', 0.20),       # 20% - signed
    ('SIGNATURE_PENDING', 0.10),      # 10% - pending signature
    ('NEEDS_RESEND', 0.10),           # 10% - needs resend
    ('CALLBACK_REQUESTED', 0.08),     # 8% - callback requested
    ('WRONG_CONTACT', 0.07),          # 7% - wrong contact
    ('NO_DECISION', 0.05),            # 5% - no decision
    ('REFUSED_INFO', 0.03),           # 3% - refused
    ('ERROR', 0.02),                  # 2% - error
]

# Outcome notes (AI-style summaries)
OUTCOME_NOTES = {
    'CONFIRMED_RECEIVED': [
        "Spoke with office staff who confirmed receipt of the POC fax. They will review and sign within 24-48 hours.",
        "Connected with Dr. {doctor}'s medical assistant. POC received and added to signing queue.",
        "Office manager confirmed fax arrived. Doctor will review during afternoon chart time.",
    ],
    'CONFIRMED_SIGNED': [
        "Dr. {doctor} personally confirmed POC has been signed and faxed back.",
        "Medical assistant verified signed POC was returned via fax this morning.",
        "Confirmation received - signed POC on file. Patient {patient} cleared for continued therapy.",
    ],
    'SIGNATURE_PENDING': [
        "POC is with Dr. {doctor} for review. Expected signature by end of week.",
        "Office confirmed doctor has the POC but hasn't signed yet. Will follow up in 2 days.",
        "Document in doctor's signing pile. Staff estimates 3-5 business days for completion.",
    ],
    'NEEDS_RESEND': [
        "Office reports they never received the original fax. Requested resend to {phone}.",
        "Fax was received but pages were illegible. Please resend at higher quality.",
        "Wrong patient information on POC. Needs to be corrected and resent.",
    ],
    'CALLBACK_REQUESTED': [
        "Dr. {doctor} wants to discuss patient case before signing. Please call back tomorrow at 2pm.",
        "Office requested callback - they have questions about the therapy plan.",
        "Staff asked us to call back after 3pm when the doctor is available.",
    ],
    'WRONG_CONTACT': [
        "This number is no longer Dr. {doctor}'s office. Patient may need updated provider info.",
        "Reached wrong medical practice. Number appears to be reassigned.",
        "Office confirmed Dr. {doctor} is no longer at this location. Retired last month.",
    ],
    'NO_DECISION': [
        "Spoke with staff but they couldn't confirm status. Said to call back next week.",
        "Office was unsure about the POC. Transferring to another department.",
        "No clear answer from contact. May need escalation or different approach.",
    ],
    'REFUSED_INFO': [
        "Office declined to provide information without patient present on the call.",
        "Staff refused to confirm or deny receipt, citing privacy policy.",
        "Contact would not engage with verification request.",
    ],
    'ERROR': [
        "Call connected but was immediately disconnected. Technical issue suspected.",
        "Unable to complete call due to system error. Retry recommended.",
        "Unexpected error during call processing. Manual review needed.",
    ],
}

# Call states for different outcomes
CALL_STATES_SUCCESS = ['initiated', 'ringing', 'answered', 'completed']
CALL_STATES_NO_ANSWER = ['initiated', 'ringing', 'no-answer']
CALL_STATES_BUSY = ['initiated', 'busy']
CALL_STATES_FAILED = ['initiated', 'failed']


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def check_safety():
    """Ensure we're in a safe environment to seed data."""
    app_env = os.getenv("APP_ENV", "").lower()
    seed_enable = os.getenv("SEED_ENABLE", "").lower() == "true"

    if app_env in ("development", "dev", "local") or seed_enable:
        return True

    print("ERROR: Seed script refused to run.")
    print("  - APP_ENV must be 'development', 'dev', or 'local'")
    print("  - OR set SEED_ENABLE=true")
    print(f"  Current APP_ENV={app_env}, SEED_ENABLE={os.getenv('SEED_ENABLE', 'not set')}")
    return False


def deterministic_uuid(seed_string: str) -> uuid.UUID:
    """Generate a deterministic UUID from a seed string for idempotency."""
    hash_bytes = hashlib.md5(seed_string.encode()).digest()
    return uuid.UUID(bytes=hash_bytes)


def weighted_choice(distribution: list) -> str:
    """Select an item based on weighted probability distribution."""
    total = sum(weight for _, weight in distribution)
    r = random.uniform(0, total)
    cumulative = 0
    for item, weight in distribution:
        cumulative += weight
        if r <= cumulative:
            return item
    return distribution[-1][0]  # Fallback


def generate_outcome_note(outcome_code: str, doctor_name: str, patient_alias: str, phone: str) -> str:
    """Generate a realistic outcome note."""
    templates = OUTCOME_NOTES.get(outcome_code, ["Call completed."])
    template = random.choice(templates)
    return template.format(
        doctor=doctor_name.replace("Dr. ", ""),
        patient=patient_alias,
        phone=phone
    )


def generate_fake_twilio_sid() -> str:
    """Generate a fake Twilio-style call SID."""
    return f"CA{uuid.uuid4().hex[:32].upper()}"


# =============================================================================
# SEED DATA GENERATION
# =============================================================================

async def get_org_by_id_or_slug(session: AsyncSession, org_identifier: str) -> Organization:
    """Get organization by UUID or slug."""
    # Try as UUID first
    try:
        org_id = uuid.UUID(org_identifier)
        result = await session.execute(
            select(Organization).where(Organization.id == org_id)
        )
        org = result.scalar_one_or_none()
        if org:
            return org
    except ValueError:
        pass  # Not a valid UUID, try slug

    # Try as slug
    result = await session.execute(
        select(Organization).where(Organization.slug == org_identifier)
    )
    org = result.scalar_one_or_none()
    if org:
        return org

    raise ValueError(f"Organization not found: {org_identifier}")


async def delete_seeded_data(session: AsyncSession, org_id: uuid.UUID):
    """Delete previously seeded data (idempotency)."""
    # Delete activity logs for seeded tasks
    await session.execute(
        delete(ActivityLog).where(
            ActivityLog.org_id == org_id,
            ActivityLog.summary.like(f"{SEED_PREFIX}%")
        )
    )

    # Delete call events for seeded tasks (cascade will handle via task deletion)
    # Delete tasks with SEED_ prefix in idempotency_key
    await session.execute(
        delete(Task).where(
            Task.org_id == org_id,
            Task.idempotency_key.like(f"{SEED_PREFIX}%")
        )
    )

    await session.commit()
    print(f"Deleted existing seeded data for org {org_id}")


async def create_seeded_tasks(
    session: AsyncSession,
    org_id: uuid.UUID,
    count: int,
    days_back: int
) -> list[Task]:
    """Create seeded tasks with realistic distribution."""
    tasks = []
    now = datetime.now(timezone.utc)

    # Status distribution: 80% resolved, 10% queued, 10% failed
    status_distribution = [
        ('RESOLVED', 0.80),
        ('QUEUED', 0.10),
        ('FAILED', 0.10),
    ]

    for i in range(count):
        # Deterministic values for idempotency
        seed_key = f"{SEED_PREFIX}task_{i:04d}"
        task_id = deterministic_uuid(f"{org_id}_{seed_key}")

        # Random but reproducible selections
        random.seed(f"{org_id}_{i}")

        workflow = random.choice(WORKFLOW_TYPES)
        doctor_name = random.choice(DOCTOR_NAMES)
        doctor_phone = random.choice(DOCTOR_PHONES)
        patient_alias = PATIENT_ALIASES[i % len(PATIENT_ALIASES)]
        therapist_phone = random.choice(THERAPIST_PHONES)

        # Spread creation dates over the time period
        days_ago = random.randint(0, days_back)
        hours_ago = random.randint(0, 23)
        created_at = now - timedelta(days=days_ago, hours=hours_ago)

        # Determine status
        status = weighted_choice(status_distribution)

        # Initialize outcome fields
        outcome_code = None
        outcome_note = None
        completed_at = None
        attempts = 0

        if status == 'RESOLVED':
            outcome_code = weighted_choice(OUTCOME_DISTRIBUTION)
            outcome_note = generate_outcome_note(outcome_code, doctor_name, patient_alias, doctor_phone)
            # Completed 1-48 hours after creation
            completion_hours = random.randint(1, 48)
            completed_at = created_at + timedelta(hours=completion_hours)
            attempts = 1
        elif status == 'FAILED':
            # Failed tasks have an error outcome
            outcome_code = 'ERROR'
            outcome_note = "Multiple call attempts failed. Manual intervention required."
            completed_at = created_at + timedelta(hours=random.randint(2, 24))
            attempts = random.randint(2, 3)
        # QUEUED tasks have no outcome yet

        task = Task(
            id=task_id,
            org_id=org_id,
            workflow_type=workflow,
            patient_alias=patient_alias,
            doctor_name=doctor_name,
            doctor_phone=doctor_phone,
            therapist_phone=therapist_phone,
            status=status,
            attempts=attempts,
            idempotency_key=seed_key,
            outcome_code=outcome_code,
            outcome_note=outcome_note,
            completed_at_utc=completed_at,
            created_at_utc=created_at,
        )
        session.add(task)
        tasks.append(task)

    await session.commit()
    print(f"Created {len(tasks)} seeded tasks")
    return tasks


async def create_call_events_for_tasks(
    session: AsyncSession,
    tasks: list[Task]
):
    """Create realistic call event history for each task."""
    call_events = []

    for task in tasks:
        random.seed(f"{task.id}_calls")

        if task.status == 'QUEUED':
            # No calls yet for queued tasks
            continue

        # Determine call pattern based on outcome
        if task.outcome_code in ['CONFIRMED_RECEIVED', 'CONFIRMED_SIGNED', 'SIGNATURE_PENDING',
                                  'CALLBACK_REQUESTED', 'NEEDS_RESEND', 'NO_DECISION', 'REFUSED_INFO']:
            # Successful connection
            states = CALL_STATES_SUCCESS
            duration = random.randint(45, 180)  # 45 seconds to 3 minutes
        elif task.outcome_code == 'WRONG_CONTACT':
            # Connected but wrong number
            states = CALL_STATES_SUCCESS
            duration = random.randint(15, 45)  # Short call
        elif task.outcome_code == 'ERROR':
            # Multiple failed attempts
            num_attempts = task.attempts
            for attempt in range(num_attempts):
                attempt_states = random.choice([CALL_STATES_NO_ANSWER, CALL_STATES_BUSY, CALL_STATES_FAILED])
                base_time = task.created_at_utc + timedelta(hours=attempt * 2)

                for j, state in enumerate(attempt_states):
                    event_time = base_time + timedelta(seconds=j * 5)
                    call_event = CallEvent(
                        id=deterministic_uuid(f"{task.id}_call_{attempt}_{j}"),
                        org_id=task.org_id,
                        task_id=task.id,
                        twilio_sid=generate_fake_twilio_sid(),
                        state=state,
                        duration_sec=None,
                        created_at_utc=event_time,
                    )
                    session.add(call_event)
                    call_events.append(call_event)
            continue
        else:
            # Default: answered call
            states = CALL_STATES_SUCCESS
            duration = random.randint(30, 120)

        # Create call event trail
        base_time = task.created_at_utc + timedelta(minutes=random.randint(5, 60))

        for j, state in enumerate(states):
            event_time = base_time + timedelta(seconds=j * 5)

            # Only the final 'completed' state has duration
            event_duration = duration if state == 'completed' else None

            call_event = CallEvent(
                id=deterministic_uuid(f"{task.id}_call_0_{j}"),
                org_id=task.org_id,
                task_id=task.id,
                twilio_sid=generate_fake_twilio_sid(),
                state=state,
                duration_sec=event_duration,
                created_at_utc=event_time,
            )
            session.add(call_event)
            call_events.append(call_event)

    await session.commit()
    print(f"Created {len(call_events)} call events")
    return call_events


async def create_activity_logs_for_tasks(
    session: AsyncSession,
    tasks: list[Task]
):
    """Create activity log entries for seeded tasks."""
    activity_logs = []

    for task in tasks:
        # Task created event
        created_log = ActivityLog(
            id=deterministic_uuid(f"{task.id}_activity_created"),
            org_id=task.org_id,
            event_type="task.created",
            task_id=task.id,
            summary=f"{SEED_PREFIX}Task created for {task.patient_alias} - {task.doctor_name}",
            details={
                "workflow_type": task.workflow_type,
                "doctor_name": task.doctor_name,
                "patient_alias": task.patient_alias,
            },
            created_at_utc=task.created_at_utc,
        )
        session.add(created_log)
        activity_logs.append(created_log)

        if task.status == 'RESOLVED' and task.outcome_code:
            # Call summary event
            summary_time = task.completed_at_utc or (task.created_at_utc + timedelta(hours=1))
            summary_log = ActivityLog(
                id=deterministic_uuid(f"{task.id}_activity_summary"),
                org_id=task.org_id,
                event_type="call.summary",
                task_id=task.id,
                summary=f"{SEED_PREFIX}Call completed: {task.outcome_code}",
                details={
                    "outcome_code": task.outcome_code,
                    "summary": task.outcome_note,
                    "doctor_name": task.doctor_name,
                    "patient_alias": task.patient_alias,
                },
                created_at_utc=summary_time,
            )
            session.add(summary_log)
            activity_logs.append(summary_log)

        if task.status == 'FAILED':
            # Failed task event
            failed_time = task.completed_at_utc or (task.created_at_utc + timedelta(hours=2))
            failed_log = ActivityLog(
                id=deterministic_uuid(f"{task.id}_activity_failed"),
                org_id=task.org_id,
                event_type="task.updated",
                task_id=task.id,
                summary=f"{SEED_PREFIX}Task failed after {task.attempts} attempts",
                details={
                    "status": "FAILED",
                    "attempts": task.attempts,
                    "doctor_name": task.doctor_name,
                },
                created_at_utc=failed_time,
            )
            session.add(failed_log)
            activity_logs.append(failed_log)

    await session.commit()
    print(f"Created {len(activity_logs)} activity log entries")
    return activity_logs


# =============================================================================
# MAIN
# =============================================================================

async def seed_data(org_identifier: str, count: int, days_back: int):
    """Main seeding function."""
    DATABASE_URL = os.getenv(
        "POSTGRES_URL",
        "postgresql+asyncpg://zackfield@localhost:5432/clinicwire"
    )

    engine = create_async_engine(DATABASE_URL, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        # Get organization
        try:
            org = await get_org_by_id_or_slug(session, org_identifier)
            print(f"Seeding data for organization: {org.name} ({org.id})")
        except ValueError as e:
            print(f"ERROR: {e}")
            await engine.dispose()
            return

        # Delete existing seeded data (idempotency)
        await delete_seeded_data(session, org.id)

        # Create tasks
        tasks = await create_seeded_tasks(session, org.id, count, days_back)

        # Create call events
        await create_call_events_for_tasks(session, tasks)

        # Create activity logs
        await create_activity_logs_for_tasks(session, tasks)

        # Summary
        print("\n" + "=" * 60)
        print("SEED COMPLETE")
        print("=" * 60)

        resolved = sum(1 for t in tasks if t.status == 'RESOLVED')
        queued = sum(1 for t in tasks if t.status == 'QUEUED')
        failed = sum(1 for t in tasks if t.status == 'FAILED')

        print(f"Tasks: {len(tasks)} total")
        print(f"  - RESOLVED: {resolved}")
        print(f"  - QUEUED: {queued}")
        print(f"  - FAILED: {failed}")
        print(f"Organization: {org.name} (slug: {org.slug})")
        print(f"Date range: last {days_back} days")
        print("=" * 60)

    await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description="Seed demo data for ClinicWire")
    parser.add_argument(
        "--org",
        required=True,
        help="Organization ID (UUID) or slug (e.g., 'demo')"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=30,
        help="Number of tasks to create (default: 30)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=45,
        help="Spread tasks over this many days back (default: 45)"
    )

    args = parser.parse_args()

    # Safety check
    if not check_safety():
        sys.exit(1)

    # Run seeding
    asyncio.run(seed_data(args.org, args.count, args.days))


if __name__ == "__main__":
    main()
