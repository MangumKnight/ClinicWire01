# Call Summary Webhook

Endpoint for receiving post-call summaries from ElevenLabs, Twilio CI, or manual sources.

## Endpoint

```
POST /webhooks/call-summary
```

## Security

- **HMAC-SHA256 signature** over `timestamp + raw_body`
- **Replay protection**: Timestamps older than 300 seconds are rejected
- **Tenant isolation**: `org_id` derived from `call_sid` lookup, never trusted from payload
- **Idempotency**: Duplicate summaries for same task return 200 without re-processing
- **PHI safety**: Phone numbers and emails are masked before storage

## Required Headers

| Header | Description |
|--------|-------------|
| `Content-Type` | `application/json` |
| `X-Webhook-Timestamp` | Unix timestamp (seconds) when request was sent |
| `X-Webhook-Signature` | `sha256=<HMAC-SHA256(secret, timestamp + body)>` |

## Payload

```json
{
  "call_sid": "CA123...",               // Required - Twilio Call SID
  "outcome_code": "CONFIRMED_RECEIVED", // Required - from allowlist
  "summary": "Office confirmed...",     // Required - max 500 chars
  "conversation_id": "conv_abc...",     // Optional - ElevenLabs conversation ID
  "next_step": "Schedule follow-up",    // Optional - max 500 chars
  "confidence": 0.95,                   // Optional - 0.0-1.0
  "source": "elevenlabs"                // Optional - source identifier
}
```

## Valid Outcome Codes (v1)

| Code | Description |
|------|-------------|
| `CONFIRMED_RECEIVED` | Office confirmed they got fax |
| `CONFIRMED_SIGNED` | POC is signed and ready |
| `SIGNATURE_PENDING` | Received, signature in progress |
| `NEEDS_RESEND` | Need to resend fax/documents |
| `CALLBACK_REQUESTED` | Asked us to call back later |
| `WRONG_CONTACT` | Wrong number or wrong person |
| `REFUSED_INFO` | Refused to provide information |
| `NO_DECISION` | Call completed, no clear outcome |
| `ERROR` | System/technical error |

## Environment Variable

```bash
# Generate with: openssl rand -hex 32
CALL_SUMMARY_WEBHOOK_SECRET=your-secret-here
```

---

## Local Testing

### 1. Generate Signature (Python)

```python
import hmac
import hashlib
import json
import time

# Configuration
SECRET = "test-secret-for-local-dev"
TIMESTAMP = str(int(time.time()))

# Payload
payload = {
    "call_sid": "CA_TEST_123",
    "outcome_code": "CONFIRMED_RECEIVED",
    "summary": "Office confirmed receipt of fax for patient John D.",
    "next_step": "No action required",
    "confidence": 0.92,
    "source": "manual"
}

# Generate signature
body = json.dumps(payload)
signature = hmac.new(
    SECRET.encode(),
    (TIMESTAMP.encode() + body.encode()),
    hashlib.sha256
).hexdigest()

print(f"Timestamp: {TIMESTAMP}")
print(f"Signature: sha256={signature}")
print(f"Body: {body}")
```

### 2. Generate Signature (Node.js)

```javascript
const crypto = require('crypto');

const SECRET = 'test-secret-for-local-dev';
const TIMESTAMP = Math.floor(Date.now() / 1000).toString();

const payload = {
  call_sid: 'CA_TEST_123',
  outcome_code: 'CONFIRMED_RECEIVED',
  summary: 'Office confirmed receipt of fax for patient John D.',
  next_step: 'No action required',
  confidence: 0.92,
  source: 'manual'
};

const body = JSON.stringify(payload);
const signature = crypto
  .createHmac('sha256', SECRET)
  .update(TIMESTAMP + body)
  .digest('hex');

console.log(`Timestamp: ${TIMESTAMP}`);
console.log(`Signature: sha256=${signature}`);
console.log(`Body: ${body}`);
```

### 3. Curl Command

First, generate the signature using the Python/Node script above, then:

```bash
# Replace TIMESTAMP and SIGNATURE with values from the script
curl -X POST http://localhost:8001/webhooks/call-summary \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Timestamp: TIMESTAMP" \
  -H "X-Webhook-Signature: sha256=SIGNATURE" \
  -d '{"call_sid":"CA_TEST_123","outcome_code":"CONFIRMED_RECEIVED","summary":"Office confirmed receipt of fax for patient John D.","next_step":"No action required","confidence":0.92,"source":"manual"}'
```

### 4. One-Liner with Inline Signature Generation

```bash
# Set your test secret
export WEBHOOK_SECRET="test-secret-for-local-dev"
export CALL_SID="CA_TEST_123"

# Generate timestamp and signature inline
TS=$(date +%s) && \
BODY='{"call_sid":"'$CALL_SID'","outcome_code":"CONFIRMED_RECEIVED","summary":"Office confirmed receipt","confidence":0.9}' && \
SIG=$(echo -n "${TS}${BODY}" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $2}') && \
curl -X POST http://localhost:8001/webhooks/call-summary \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Timestamp: $TS" \
  -H "X-Webhook-Signature: sha256=$SIG" \
  -d "$BODY"
```

---

## Expected Results

### Database Updates

After a successful POST:

1. **tasks table**:
   - `outcome_code` = `"CONFIRMED_RECEIVED"`
   - `outcome_note` = `"Office confirmed receipt of fax for patient John D.\n\nNext step: No action required"`
   - `completed_at_utc` = current timestamp

2. **activity_log table**:
   - `event_type` = `"call.summary"`
   - `summary` = `"Call summary received: CONFIRMED_RECEIVED"`
   - `task_id` = linked task ID
   - `details` = JSON with masked summary, outcome_code, confidence, source

### Verify in Database

```sql
-- Check task outcome
SELECT id, outcome_code, outcome_note, completed_at_utc
FROM tasks
WHERE id = '<task_id>';

-- Check activity log
SELECT id, event_type, summary, details, created_at_utc
FROM activity_log
WHERE task_id = '<task_id>' AND event_type = 'call.summary';
```

### Verify in UI

1. Open `http://localhost:8001/poc-calling-mcp.html`
2. Navigate to the **Activity** tab
3. Filter by "All" or "Calls"
4. Look for event: `"Call summary received: CONFIRMED_RECEIVED"`

---

## Demo Flow (SIMULATE=true)

```bash
# 1. Start the backend
cd backend
SIMULATE=true CALL_SUMMARY_WEBHOOK_SECRET=test-secret python3 -m uvicorn main:app --port 8001

# 2. Create a task via UI or note an existing task's call_sid from call_events table

# 3. Get a valid call_sid from the database
psql -d clinicwire -c "SELECT twilio_sid FROM call_events ORDER BY created_at_utc DESC LIMIT 1;"

# 4. POST the summary (use the one-liner above with the real call_sid)

# 5. Verify task was updated
psql -d clinicwire -c "SELECT outcome_code, outcome_note FROM tasks WHERE id = (SELECT task_id FROM call_events WHERE twilio_sid = 'CA_TEST_123');"
```

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| 403 Invalid signature | Wrong secret or signature mismatch | Verify `CALL_SUMMARY_WEBHOOK_SECRET` matches |
| 403 Timestamp too old | Timestamp > 300s ago | Generate fresh timestamp |
| 400 Invalid outcome_code | Code not in allowlist | Use valid code from list above |
| 404 Call event not found | `call_sid` doesn't exist | Ensure call_event exists with that twilio_sid |
| 200 "Summary already processed" | Idempotency - already received | Expected for duplicates |
