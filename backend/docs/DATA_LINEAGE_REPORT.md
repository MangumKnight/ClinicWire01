# Data Lineage Report
## ClinicWire Dashboard Pipeline Analysis

**Generated:** 2026-01-29
**Purpose:** Document every data point from event creation through UI display
**Scope:** Tasks tab, Activity tab, Impact tab

---

## Executive Summary

### Key Finding: 1 Critical Bug Must Be Fixed Before Seeding

| Severity | Issue | Fix |
|----------|-------|-----|
| **CRITICAL** | `CallEvent.org_id` not populated in production code | Update `repo_v2.py:create_call_event()` to accept and set `org_id` |

### What's Working

- Tenant isolation via `org_id.in_(auth.org_ids)` on all API endpoints
- Dashboard metrics calculations match test assertions
- SUCCESS_OUTCOME_CODES properly defined: `CONFIRMED_RECEIVED`, `CONFIRMED_SIGNED`, `SIGNATURE_PENDING`
- Activity log idempotency for `call.summary` webhook
- Date range defaults to last 30 days

### Pipeline Status

```
Event Creation -> Database -> API -> UI
     [OK]           [BUG]     [OK]   [OK]

BUG: CallEvent created without org_id in repo layer
```

### Action Required

1. Fix `repo_v2.py:create_call_event()` to accept `org_id` parameter
2. Update 4 call sites in `main.py` (lines 877, 915, 1144, 1167)
3. Then proceed with seed data

---

## 1. Data Model Inventory (Source of Truth)

### Core Tenant Tables

| Table | Primary Key | Tenant Column | Key Fields for Dashboard |
|-------|-------------|---------------|--------------------------|
| `tasks` | `id` (UUID) | `org_id` | `status`, `outcome_code`, `outcome_note`, `completed_at_utc`, `created_at_utc`, `attempts` |
| `call_events` | `id` (UUID) | `org_id` | `state`, `duration_sec`, `created_at_utc`, `task_id`, `twilio_sid` |
| `sms_events` | `id` (UUID) | `org_id` | `status`, `type`, `created_at_utc`, `task_id` |
| `activity_log` | `id` (UUID) | `org_id` | `event_type`, `summary`, `details`, `task_id`, `created_at_utc` |
| `contacts` | `id` (UUID) | `org_id` | `doctor_name`, `phone_e164`, `office_name` |

### Auth Tables (No org_id)

| Table | Purpose |
|-------|---------|
| `orgs` | Organization definitions |
| `users` | User accounts |
| `org_memberships` | User <-> Org junction (role) |
| `user_sessions` | JWT session tracking |
| `auth_codes` | Magic link codes |

### Key Timestamp Fields

| Field | Table | Usage |
|-------|-------|-------|
| `created_at_utc` | tasks | Task creation time, date range filtering |
| `completed_at_utc` | tasks | Resolution time, avg_days_to_resolution calculation |
| `created_at_utc` | call_events | Call time, date range filtering for calls_attempted |
| `created_at_utc` | activity_log | Activity timestamp, cursor-based pagination |

---

## 2. UI -> API Mapping

### Tasks Tab

| UI Element | API Endpoint | HTTP Method | Auth Required |
|------------|--------------|-------------|---------------|
| Task list | `/tasks` | GET | Yes |
| Create task | `/tasks` | POST | Yes |
| Execute call | `/tasks/{id}/call` | POST | Yes |
| Task status update | (via webhooks) | N/A | Webhook signature |

### Activity Tab

| UI Element | API Endpoint | HTTP Method | Auth Required |
|------------|--------------|-------------|---------------|
| Activity feed | `/api/activity` | GET | Yes |
| Load more (cursor) | `/api/activity?cursor=X` | GET | Yes |
| Filter by type | `/api/activity?event_type=X` | GET | Yes |

### Impact Tab

| UI Element | API Endpoint | HTTP Method | Auth Required |
|------------|--------------|-------------|---------------|
| Summary metrics | `/api/dashboard/summary` | GET | Yes |
| Outcome distribution | `/api/dashboard/outcomes` | GET | Yes |
| Date range filter | Query params: `start_date`, `end_date` | - | - |

### Contacts Modal

| UI Element | API Endpoint | HTTP Method | Auth Required |
|------------|--------------|-------------|---------------|
| Search contacts | `/api/contacts` | GET | Yes |
| Create contact | `/api/contacts` | POST | Yes |
| Update contact | `/api/contacts/{id}` | PATCH | Yes |
| Delete contact | `/api/contacts/{id}` | DELETE | Yes |

---

## 3. Event Creation Points

### Where Data Gets Written

| Event Type | Trigger | Table(s) Written | Location in Code |
|------------|---------|------------------|------------------|
| Task created | POST /tasks | `tasks`, `activity_log` | `main.py:217-224` |
| Call initiated | POST /tasks/{id}/call | `call_events`, `activity_log` | `main.py:886-892`, `main.py:924-930` |
| Call completed | Twilio status webhook | `call_events` (update), `activity_log` | `main.py:476-489` |
| Call failed | Twilio status webhook | `call_events` (update), `activity_log` | `main.py:476-489` |
| Call no-answer | Twilio status webhook | `call_events` (update), `activity_log`, `tasks` (status) | `main.py:476-489` |
| SMS sent | After call completion | `sms_events`, `activity_log`, `tasks` (last_sms_sent_at) | `main.py:530-537` |
| Call summary | ElevenLabs webhook | `tasks` (outcome_code, outcome_note, completed_at_utc), `activity_log` | `main.py:817-831` |
| Task status change | Internal processing | `tasks`, `activity_log` | Various locations |

### Activity Log Event Types (from models_multitenant.py:497-500)

```
task.created, task.status_changed, task.deleted
call.initiated, call.completed, call.no_answer, call.failed
sms.sent, sms.delivered, sms.failed
call.summary (from ElevenLabs webhook)
```

---

## 4. Tenant Isolation Proof

### API Layer Isolation

| Endpoint | Isolation Method | Code Reference |
|----------|------------------|----------------|
| `/api/dashboard/summary` | `Task.org_id.in_(auth.org_ids)` | `dashboard.py:144,167` |
| `/api/dashboard/outcomes` | `Task.org_id.in_(auth.org_ids)` | `dashboard.py:253-254` |
| `/api/activity` | `ActivityLog.org_id.in_(auth.org_ids)` | `activity.py:106` |
| `/tasks` | `Task.org_id.in_(auth.org_ids)` | `repo_v2.py:195` |
| `/api/contacts` | `Contact.org_id.in_(org_ids)` | `repo_v2.py:358` |

### auth.org_ids Source

```python
# auth/jwt_handler.py - get_current_user()
# Returns AuthContext with org_ids list derived from JWT token
# JWT contains user_id -> query org_memberships -> return list of org_ids
```

### Webhook Isolation (System Calls)

| Webhook | Isolation Method |
|---------|------------------|
| Twilio status | Lookup by `twilio_sid` -> get `task.org_id` |
| ElevenLabs summary | Lookup by `twilio_sid` -> get `task.org_id` |

---

## 5. Date/Time Logic

### Default Date Range

```python
# dashboard.py:104-113
def get_date_range(start_date, end_date):
    if end_date is None:
        end_date = datetime.now(timezone.utc)
    if start_date is None:
        start_date = end_date - timedelta(days=30)
    return start_date, end_date
```

### Metric Date Filtering

| Metric | Date Field Used | Filter Logic |
|--------|-----------------|--------------|
| calls_attempted | `CallEvent.created_at_utc` | `>= start AND <= end` |
| calls_connected | `CallEvent.created_at_utc` | `>= start AND <= end` |
| successful_outcomes | `Task.created_at_utc` AND `Task.completed_at_utc` | Both must be in range |
| avg_days_to_resolution | `Task.created_at_utc`, `Task.completed_at_utc` | `created_at_utc` in range, `completed_at_utc` not null and in range |
| outcome_distribution | `Task.created_at_utc` | `>= start AND <= end` |

### Timezone Handling

- All `*_utc` fields use `DateTime(timezone=True)`
- Server stores in UTC
- UI sends/receives ISO 8601 strings
- No timezone conversion needed in queries

---

## 6. Outcome Code Logic

### Success Definition (Single Source of Truth)

```python
# dashboard.py:36
SUCCESS_OUTCOME_CODES = ['CONFIRMED_RECEIVED', 'CONFIRMED_SIGNED', 'SIGNATURE_PENDING']
```

### All Known Outcome Codes

| Code | Label | Is Success? | Set By |
|------|-------|-------------|--------|
| `CONFIRMED_RECEIVED` | Confirmed Receipt | YES | ElevenLabs call summary |
| `CONFIRMED_SIGNED` | Signed | YES | ElevenLabs call summary |
| `SIGNATURE_PENDING` | Pending Signature | YES | ElevenLabs call summary |
| `NEEDS_RESEND` | Needs Resend | NO | ElevenLabs call summary |
| `CALLBACK_REQUESTED` | Callback Requested | NO | ElevenLabs call summary |
| `WRONG_CONTACT` | Wrong Contact | NO | ElevenLabs call summary |
| `REFUSED_INFO` | Refused | NO | ElevenLabs call summary |
| `NO_DECISION` | No Decision | NO | ElevenLabs call summary |
| `ERROR` | Error | NO | System (call failures) |

### Outcome Code Labels (UI Display)

```python
# dashboard.py:39-49
OUTCOME_LABELS = {
    'CONFIRMED_RECEIVED': 'Confirmed Receipt',
    'CONFIRMED_SIGNED': 'Signed',
    'SIGNATURE_PENDING': 'Pending Signature',
    'NEEDS_RESEND': 'Needs Resend',
    'CALLBACK_REQUESTED': 'Callback Requested',
    'WRONG_CONTACT': 'Wrong Contact',
    'REFUSED_INFO': 'Refused',
    'NO_DECISION': 'No Decision',
    'ERROR': 'Error'
}
```

### Call States (for calls_connected)

```python
# Connected = state IN ('completed', 'answered')
# All other states: 'initiated', 'ringing', 'in-progress', 'busy', 'no-answer', 'failed', 'canceled'
```

---

## 7. Idempotency and Duplication Risks

### Task Creation Idempotency

```python
# repo_v2.py:22-30
# Idempotency key = SHA256(patient_alias + doctor_name + doctor_phone + workflow_type + date)
# Unique constraint: (org_id, idempotency_key)
```

**Risk:** Same task created twice on same day with identical inputs -> returns existing task
**Mitigation:** Working as designed

### Activity Log Idempotency

```python
# repo_v2.py:487-498
async def has_event_for_task(self, task_id, event_type) -> bool:
    # Checks if event_type already exists for task_id
```

**Risk:** Webhook retry could log duplicate activity
**Mitigation:** `has_event_for_task()` check before logging
**Gap:** Not all activity log writes use this check (see Gaps section)

### SMS Deduplication

```python
# models_multitenant.py:406-407
UniqueConstraint('task_id', 'type', name='uq_sms_events_task_type')
```

**Risk:** Duplicate SMS for same task/type -> IntegrityError
**Mitigation:** Unique constraint prevents duplicates, rollback returns None

### CallEvent Creation

```python
# repo_v2.py:245-255
# No idempotency check - creates new event each time
```

**Risk:** If call initiation retries, could create duplicate call_events
**Mitigation:** `twilio_sid` is unique per call, updates use `twilio_sid` lookup

---

## 8. Gaps and Mismatches to Fix Before Seeding

### CRITICAL GAPS

| ID | Issue | Impact | Recommendation |
|----|-------|--------|----------------|
| GAP-1 | **CallEvent.org_id NOT SET by repo** | `create_call_event()` in `repo_v2.py:245-255` does NOT accept or set org_id. Called at `main.py:877,915,1144,1167` | **FIX REQUIRED**: Update `create_call_event()` to accept org_id parameter and set it on the event |

### INVESTIGATION RESULTS

| ID | Issue | Finding | Status |
|----|-------|---------|--------|
| INV-1 | CallEvent org_id source | **CONFIRMED BUG**: `create_call_event(task_id, state, twilio_sid)` never receives org_id. Tests manually set org_id but production code doesn't. | **FIX REQUIRED** |
| INV-2 | Activity log idempotency | Only `call.summary` (main.py:792) uses `has_event_for_task()`. Other events (`task.created`, `call.initiated`, `sms.sent`) don't need it - they're called inline (not from retryable webhooks). | **ACCEPTABLE** |

### ACTIVITY LOG IDEMPOTENCY ANALYSIS

| Event Type | Location | Has Idempotency Check? | Needed? |
|------------|----------|------------------------|---------|
| `task.created` | main.py:217 | No | No - called once per POST /tasks |
| `call.initiated` | main.py:886,924 | No | No - called once per execute_call |
| `call.completed/failed/no_answer` | main.py:482 | No | Maybe - Twilio webhooks can retry |
| `sms.sent` | main.py:530 | No | No - only called after success check |
| `call.summary` | main.py:817 | **YES** (main.py:792) | Yes - ElevenLabs webhook retries |

### VERIFIED WORKING

| Item | Status |
|------|--------|
| Task tenant isolation | Verified - all queries use `org_id.in_(auth.org_ids)` |
| Dashboard metrics SQL | Verified - matches test assertions |
| SUCCESS_OUTCOME_CODES includes SIGNATURE_PENDING | Verified |
| Date range defaults to 30 days | Verified |
| Outcome labels match codes | Verified |
| call.summary idempotency | Verified - uses has_event_for_task() |

---

## 9. Metric Calculation Formulas

### Summary Metrics (dashboard.py)

```python
# Calls Attempted = COUNT(call_events) in date range
calls_attempted = COUNT(CallEvent.id) WHERE org_id IN auth.org_ids AND created_at_utc BETWEEN start AND end

# Calls Connected = call_events with connected state
calls_connected = COUNT(CallEvent.id) WHERE ... AND state IN ('completed', 'answered')

# Successful Outcomes = tasks with success outcome AND completion in range
successful_outcomes = COUNT(Task.id) WHERE org_id IN auth.org_ids
    AND outcome_code IN SUCCESS_OUTCOME_CODES
    AND completed_at_utc IS NOT NULL
    AND completed_at_utc BETWEEN start AND end
    AND created_at_utc BETWEEN start AND end

# Avg Days to Resolution
avg_days = AVG(EXTRACT(epoch FROM (completed_at_utc - created_at_utc)) / 86400)
    WHERE completed_at_utc IS NOT NULL AND completed_at_utc BETWEEN start AND end

# Estimated Hours Saved
estimated_hours = (calls_attempted * DEFAULT_MINUTES_SAVED_PER_CALL) / 60

# Estimated Cost Saved
estimated_cost = estimated_hours * DEFAULT_HOURLY_RATE
```

### Backend Constants

```python
DEFAULT_MINUTES_SAVED_PER_CALL = 15  # Configurable via env
DEFAULT_HOURLY_RATE = 35             # Configurable via env
```

---

## 10. Pre-Seed Checklist

Before adding demo/seed data:

### Required Code Fixes

- [ ] **FIX GAP-1**: Update `repo_v2.py:create_call_event()` to accept and set `org_id`:
  ```python
  async def create_call_event(self, task_id: uuid.UUID, org_id: uuid.UUID, state: str, twilio_sid: Optional[str] = None) -> CallEvent:
      event = CallEvent(
          org_id=org_id,  # ADD THIS
          task_id=task_id,
          state=state,
          twilio_sid=twilio_sid
      )
  ```
- [ ] Update all callers in `main.py` to pass `org_id` (lines 877, 915, 1144, 1167)

### Seed Data Requirements

- [ ] Seed data must include:
  - [ ] Organization(s) with known IDs
  - [ ] User(s) with org memberships
  - [ ] Tasks with various outcome_codes (including all SUCCESS_OUTCOME_CODES)
  - [ ] Tasks with completed_at_utc set (for avg_days calculation)
  - [ ] CallEvents with states: completed, answered, no-answer, failed, etc.
  - [ ] CallEvents with created_at_utc in the date range
  - [ ] Activity log entries for timeline testing
- [ ] Run test_dashboard.py to verify calculations
- [ ] Manually verify Impact tab renders with seed data

---

## Appendix A: File Reference

| File | Relevant Content |
|------|------------------|
| `db/models_multitenant.py` | All table definitions, constraints, indexes |
| `db/repo_v2.py` | Repository layer, idempotency logic |
| `routes/dashboard.py` | Dashboard API endpoints, metric calculations |
| `routes/activity.py` | Activity API endpoint |
| `main.py` | Event creation points, webhook handlers |
| `poc-calling-mcp.html` | UI fetch calls, tab rendering |
| `tests/test_dashboard.py` | Dashboard calculation tests |

---

## Appendix B: SQL Query Examples

### Calls Attempted (dashboard.py:150-153)
```sql
SELECT COUNT(id) FROM call_events
WHERE org_id IN (:org_ids)
  AND created_at_utc >= :start
  AND created_at_utc <= :end;
```

### Successful Outcomes (dashboard.py:175-186)
```sql
SELECT COUNT(id) FROM tasks
WHERE org_id IN (:org_ids)
  AND created_at_utc >= :start
  AND created_at_utc <= :end
  AND outcome_code IN ('CONFIRMED_RECEIVED', 'CONFIRMED_SIGNED', 'SIGNATURE_PENDING')
  AND completed_at_utc IS NOT NULL
  AND completed_at_utc >= :start
  AND completed_at_utc <= :end;
```

### Outcome Distribution (dashboard.py:261-267)
```sql
SELECT outcome_code, COUNT(id)
FROM tasks
WHERE org_id IN (:org_ids)
  AND created_at_utc >= :start
  AND created_at_utc <= :end
  AND outcome_code IS NOT NULL
GROUP BY outcome_code
ORDER BY COUNT(id) DESC;
```
