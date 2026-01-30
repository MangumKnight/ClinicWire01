# Seed Data Guide

This document explains how to seed demo data for testing the ClinicWire UI.

## Overview

The seed script creates realistic test data including:
- **30 Tasks** with various statuses (RESOLVED, QUEUED, FAILED)
- **~130 Call Events** showing call history trails
- **60 Activity Log entries** for the Activity tab

## Prerequisites

1. Server must be running or database accessible
2. Demo organization must exist (created automatically on first login)
3. Environment must be safe (development mode)

## Running the Seed Script

### Basic Usage

```bash
cd backend

# Using demo org by slug
APP_ENV=development python3 scripts/seed_demo_data.py --org demo --count 30 --days 45

# Using explicit org UUID
APP_ENV=development python3 scripts/seed_demo_data.py --org 04a4c7b9-304a-4764-995c-7a1d9a8dfba9 --count 30 --days 45
```

### Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `--org` | Yes | - | Organization UUID or slug |
| `--count` | No | 30 | Number of tasks to create |
| `--days` | No | 45 | Spread tasks over this many days |

### Safety Requirements

The script will **refuse to run** unless one of these is true:
- `APP_ENV=development` (or `dev`, `local`)
- `SEED_ENABLE=true`

## What Gets Created

### Task Distribution

| Status | Percentage | Description |
|--------|------------|-------------|
| RESOLVED | ~80% | Completed tasks with outcomes |
| QUEUED | ~10% | Pending tasks |
| FAILED | ~10% | Failed after multiple attempts |

### Outcome Distribution (for RESOLVED tasks)

| Outcome Code | Percentage | Success? |
|--------------|------------|----------|
| CONFIRMED_RECEIVED | 35% | ✅ |
| CONFIRMED_SIGNED | 20% | ✅ |
| SIGNATURE_PENDING | 10% | ✅ |
| NEEDS_RESEND | 10% | ❌ |
| CALLBACK_REQUESTED | 8% | ❌ |
| WRONG_CONTACT | 7% | ❌ |
| NO_DECISION | 5% | ❌ |
| REFUSED_INFO | 3% | ❌ |
| ERROR | 2% | ❌ |

### Call Events

Each completed task gets a realistic call trail:
- `initiated` → `ringing` → `answered` → `completed`

Failed tasks get multiple failed attempts:
- `initiated` → `no-answer` (or `busy`, `failed`)

### Activity Logs

- `task.created` for every task
- `call.summary` for RESOLVED tasks (with outcome details)
- `task.updated` for FAILED tasks

## Idempotency

The script is **idempotent**:
- Uses `SEED_` prefix in idempotency keys
- Running twice deletes existing seeded data and recreates

To identify seeded data:
```sql
SELECT * FROM tasks WHERE idempotency_key LIKE 'SEED_%';
SELECT * FROM activity_log WHERE summary LIKE 'SEED_%';
```

## Verifying the Data

### 1. Check DB Row Counts

```bash
python3 << 'EOF'
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select, func
import os, sys
sys.path.insert(0, '.')
from db.models_multitenant import Task, CallEvent, ActivityLog

async def check():
    engine = create_async_engine(os.getenv("POSTGRES_URL", "postgresql+asyncpg://localhost/clinicwire"))
    async with sessionmaker(engine, class_=AsyncSession)() as s:
        tasks = (await s.execute(select(func.count(Task.id)).where(Task.idempotency_key.like('SEED_%')))).scalar()
        calls = (await s.execute(select(func.count(CallEvent.id)))).scalar()
        logs = (await s.execute(select(func.count(ActivityLog.id)).where(ActivityLog.summary.like('SEED_%')))).scalar()
        print(f"Seeded tasks: {tasks}, Call events: {calls}, Activity logs: {logs}")
    await engine.dispose()

asyncio.run(check())
EOF
```

### 2. Test Dashboard APIs

```bash
# Get a token first (login as demo@clinicwire.com)
TOKEN="your-jwt-token"

# Summary metrics
curl -s "http://localhost:8000/api/dashboard/summary" \
  -H "Authorization: Bearer $TOKEN"

# Outcome distribution
curl -s "http://localhost:8000/api/dashboard/outcomes" \
  -H "Authorization: Bearer $TOKEN"
```

### 3. Test Tasks API

```bash
curl -s "http://localhost:8000/tasks?per_page=100" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; print(f'Tasks: {json.load(sys.stdin)[\"count\"]}')"
```

### 4. Test Activity API

```bash
curl -s "http://localhost:8000/api/activity?limit=50" \
  -H "Authorization: Bearer $TOKEN" | python3 -c "import sys,json; print(f'Activities: {len(json.load(sys.stdin)[\"items\"])}')"
```

## UI Verification

After seeding, verify in the browser:

### Tasks Tab
- [ ] Tasks list shows rows
- [ ] Status column shows RESOLVED/QUEUED/FAILED
- [ ] Outcome column shows badges (CONFIRMED_RECEIVED, etc.)
- [ ] Clicking "Details" opens drawer with call history

### Activity Tab
- [ ] Shows activity events
- [ ] `call.summary` events have SUMMARY badge and outcome
- [ ] `task.created` events show task info

### Impact Tab
- [ ] Outcome distribution chart shows data
- [ ] Summary metrics (calls attempted, connected, success rate) are non-zero
- [ ] Changing date range updates the numbers

## Cleanup

To remove seeded data:

```sql
DELETE FROM activity_log WHERE summary LIKE 'SEED_%';
DELETE FROM tasks WHERE idempotency_key LIKE 'SEED_%';
-- CallEvents are cascade-deleted with tasks
```

Or re-run the seed script (it cleans up before seeding).

## Troubleshooting

### "Organization not found"
- Login first to create the demo org
- Or check the org slug/UUID is correct

### "Seed script refused to run"
- Set `APP_ENV=development` or `SEED_ENABLE=true`

### UI shows "No data" but DB has rows
- Check browser console for errors
- Verify you're logged in (token not expired)
- Check the org filter matches (user must be member of seeded org)
