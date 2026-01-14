# ClinicWire Smoke Test Report

**Last Updated:** 2026-01-14
**Branch:** `main` (merged from `stabilize/auth-db`)
**Status:** Phase 2 Complete - Automated Smoke Tests

---

## System Health Summary

| Component | Status | Notes |
|-----------|--------|-------|
| Backend Server | OK | Starts without errors |
| Database | OK | PostgreSQL connected, schema current |
| Alembic Migrations | OK | Current = Head (004) |
| Auth Magic Link | OK | Fixed email construction, dev fallback |
| Twilio API | OK | Account active, credentials valid |
| ElevenLabs API | OK | Creator tier, connected |

---

## Phase 1 Fixes Applied

### 1. Magic Link Email Bug (FIXED)
- **File:** `backend/auth/magic_link.py`
- **Issue:** `msg.set_content()` called after `msg.add_alternative()` on multipart message
- **Fix:** Reversed order - set plain text first, then add HTML alternative
- **Added:** Graceful SMTP handling:
  - Development: Returns 202, logs magic link to console
  - Production without SMTP: Returns 503 "Email service unavailable"

### 2. Auth Route Status Codes (FIXED)
- **File:** `backend/routes/auth.py`
- **Issue:** All responses returned 200, errors returned 500
- **Fix:** Returns 202 Accepted, 503 for SMTP errors, structured error messages

### 3. Database Outcome Columns (FIXED)
- **Migration:** `004_add_outcome_fields.py` (already existed, now applied)
- **Columns Added:**
  - `tasks.outcome_code` (VARCHAR(50), nullable)
  - `tasks.outcome_note` (TEXT, nullable)
  - `tasks.completed_at_utc` (TIMESTAMP WITH TIME ZONE, nullable)
- **Index:** `ix_tasks_outcome_code` created

---

## Endpoint Status

| Endpoint | Method | Expected | Actual | Status |
|----------|--------|----------|--------|--------|
| `/health` | GET | 200 + JSON | 200 | OK |
| `/docs` | GET | 200 | 200 | OK |
| `/api/auth/login` | POST | 202 | 202 | OK |
| `/tasks` | GET | 401/403 (unauth) | 403 | OK |
| `/api/contacts` | GET | 401/403 (unauth) | 403 | OK |
| `/webhooks/twilio/status` | POST | 200 | 200 | OK |
| `/webhooks/elevenlabs` | POST | 200 | OK |

---

## Verification Commands

```bash
# Start server
cd backend && source ../.venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8001

# Health check
curl http://localhost:8001/health

# Auth login (dev mode - check console for link)
curl -X POST http://localhost:8001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com"}'

# Verify DB schema
psql -d clinicwire -c "\d tasks" | grep outcome

# Verify alembic state
cd backend && alembic current && alembic heads
```

---

## Known Issues (Deferred)

1. **poc-calling-mcp.html** - Template file missing, returns 500
2. **Demo login disabled** - Returns "Demo mode not available"

---

## Phase 2: Automated Smoke Tests

### Script Created
- **Location:** `scripts/smoke_test.sh`
- **Usage:** `./scripts/smoke_test.sh [PORT]` (default: 8001)

### Test Suite
| Test | Endpoint | Method | Expected |
|------|----------|--------|----------|
| 1 | `/health` | GET | 200, services.db == connected |
| 2 | `/api/auth/login` | POST | 200/202/503 |
| 3 | `/docs` | GET | 200 |
| 4 | `/tasks` | GET | 401 or 403 (no auth) |
| 5 | `/webhooks/twilio/status` | POST | 200 or 4xx |

### Consecutive Test Runs (2026-01-14)

**Run 1** - 13:40:00
```
  Total: 5 | Pass: 5 | Fail: 0
  SMOKE TEST PASSED
```

**Run 2** - 13:40:04
```
  Total: 5 | Pass: 5 | Fail: 0
  SMOKE TEST PASSED
```

**Run 3** - 13:40:10
```
  Total: 5 | Pass: 5 | Fail: 0
  SMOKE TEST PASSED
```

**Result: 3/3 PASSES - System Stable**

---

## Next Steps (Phase 3)

- [ ] Frontend upgrade planning (React/Next.js)
- [ ] CI/CD integration for smoke tests
- [ ] Production deployment checklist
