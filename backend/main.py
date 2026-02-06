#!/usr/bin/env python3
"""
33Health MCP Server - Production Version
PostgreSQL-first architecture with proper phone handling, idempotency, and SMS deduplication
"""

import os
import logging
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, validator
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv

# Import our modules
from db.database import get_session, close_database, async_session_maker
from db.auth_session import get_auth_session
from db.repo_v2 import TaskRepository, CallEventRepository, SmsEventRepository, ActivityLogRepository
from services.twilio_service import TwilioService
from services.elevenlabs_service import ElevenLabsService
from utils.phone import normalize_us_number, format_display
from utils.schedule import is_business_hours, next_business_time, schedule_retry, validate_call_timing, log_scheduling_decision, get_business_day_key
from auth.jwt_handler import get_current_user, get_optional_user, AuthContext
from twilio.request_validator import RequestValidator

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Initialize services
twilio_service = TwilioService()
elevenlabs_service = ElevenLabsService()

# Background retry task handle
retry_task = None

# Request/Response models
class TaskRequest(BaseModel):
    workflow_type: Optional[str] = Field(default="POC_SIGNATURE")
    patient_alias: str = Field(..., min_length=1)
    doctor_name: str = Field(..., min_length=1)
    doctor_phone: str = Field(..., description="Doctor's phone number (who gets called)")
    therapist_phone: str = Field(..., description="Therapist phone (receives SMS updates)")
    notes: Optional[str] = None
    
    @validator('doctor_phone')
    def validate_doctor_phone(cls, v):
        try:
            normalized, ext = normalize_us_number(v)
            return normalized
        except ValueError as e:
            raise ValueError(f"Invalid doctor phone number: {str(e)}")
    
    @validator('therapist_phone')
    def validate_therapist_phone(cls, v):
        try:
            normalized, ext = normalize_us_number(v)
            return normalized
        except ValueError as e:
            raise ValueError(f"Invalid therapist phone number: {str(e)}")

class TaskResponse(BaseModel):
    task_id: str
    status: str
    idempotency_hit: bool = False

# Lifespan management
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle application lifecycle"""
    global retry_task
    
    # Startup
    logger.info("Starting 33Health MCP Production Server")
    
    # Start retry processor
    retry_task = asyncio.create_task(process_retries())
    
    yield
    
    # Shutdown
    logger.info("Shutting down")
    
    if retry_task:
        retry_task.cancel()
        try:
            await retry_task
        except asyncio.CancelledError:
            pass
    
    await close_database()

# Initialize FastAPI
app = FastAPI(
    title="33Health MCP API",
    version="2.0.0",
    description="Production API for POC calling automation",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("ALLOWED_ORIGINS", "*")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
from routes.contacts import router as contacts_router
from routes.auth import router as auth_router
from routes.organizations import router as organizations_router
from routes.activity import router as activity_router
from routes.dashboard import router as dashboard_router
app.include_router(contacts_router)
app.include_router(auth_router)
app.include_router(organizations_router)
app.include_router(activity_router)
app.include_router(dashboard_router)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# HTML Pages
@app.get("/")
async def root():
    """Redirect to main app or login"""
    return FileResponse("poc-calling-mcp.html")

@app.get("/login")
async def login_page():
    """Serve login page"""
    return FileResponse("templates/login.html")

@app.get("/poc-calling-mcp.html")
async def calling_interface():
    """Serve the calling interface"""
    return FileResponse("poc-calling-mcp.html")

# API Endpoints
@app.post("/tasks", response_model=TaskResponse)
async def create_task(
    request: TaskRequest,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """
    Create a new POC calling task
    - Normalizes phone to E.164
    - Enforces idempotency
    - Triggers call flow
    """
    try:
        # Get repositories
        task_repo = TaskRepository(session)
        call_event_repo = CallEventRepository(session)
        
        # Check for phone extensions
        original_doctor_phone = request.dict().get('doctor_phone', '')
        doctor_normalized, doctor_extension = normalize_us_number(original_doctor_phone)
        
        original_therapist_phone = request.dict().get('therapist_phone', '')
        therapist_normalized, therapist_extension = normalize_us_number(original_therapist_phone)
        
        # Get the user's current organization (first one for now)
        current_org = auth.org_memberships[0].org if auth.org_memberships else None
        if not current_org:
            raise HTTPException(status_code=403, detail="User not associated with any organization")
        
        # Prepare task data
        task_data = {
            'org_id': current_org.id,
            'workflow_type': request.workflow_type,
            'patient_alias': request.patient_alias.strip(),
            'doctor_name': request.doctor_name.strip(),
            'doctor_phone': doctor_normalized,
            'therapist_phone': therapist_normalized,
            'notes': request.notes or ''
        }
        
        # Add extensions to notes if present
        extensions_note = []
        if doctor_extension:
            extensions_note.append(f"Doctor ext: {doctor_extension}")
        if therapist_extension:
            extensions_note.append(f"Therapist ext: {therapist_extension}")
        if extensions_note:
            task_data['notes'] = f"{' | '.join(extensions_note)} | {task_data['notes']}".strip()
        
        # Create or get existing task (idempotency)
        task = await task_repo.create_or_get_task(task_data)

        # Check if this is a duplicate request
        idempotency_hit = task.status != 'QUEUED' or task.attempts > 0

        if not idempotency_hit:
            # Task created successfully - remains QUEUED until manual "Call Now" is pressed
            logger.info(f"[Task] Created {task.id} - waiting for manual call initiation")

            # Log activity
            activity_repo = ActivityLogRepository(session)
            await activity_repo.log_event(
                org_id=current_org.id,
                event_type="task.created",
                summary=f"Task created for {task.patient_alias} → {task.doctor_name}",
                task_id=task.id,
                actor_id=auth.user_id,
                details={"workflow_type": task.workflow_type, "status": task.status}
            )
        else:
            logger.info(f"[Task] Idempotency hit for {task.id}")
        
        return TaskResponse(
            task_id=str(task.id),
            status=task.status,
            idempotency_hit=idempotency_hit
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating task: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/tasks/{task_id}")
async def get_task(
    task_id: str,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Get task details including latest status"""
    try:
        task_repo = TaskRepository(session)
        task = await task_repo.get_by_id(uuid.UUID(task_id), auth.org_ids)

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        
        # Get call events sorted by time (newest first)
        call_events_sorted = sorted(task.call_events, key=lambda x: x.created_at_utc, reverse=True) if task.call_events else []
        latest_call = call_events_sorted[0] if call_events_sorted else None

        # Build call history (PHI-safe: no raw_status_json)
        call_history = [
            {
                "state": ce.state,
                "duration_sec": ce.duration_sec,
                "created_at": ce.created_at_utc.isoformat()
            }
            for ce in call_events_sorted
        ]

        return {
            "task_id": str(task.id),
            "status": task.status,
            "workflow_type": task.workflow_type,
            "patient_alias": task.patient_alias,
            "doctor_name": task.doctor_name,
            "doctor_phone": format_display(task.doctor_phone),
            "therapist_phone": format_display(task.therapist_phone),
            "created_at": task.created_at_utc.isoformat(),
            "updated_at": task.updated_at_utc.isoformat(),
            "attempts": task.attempts,
            "outcome_code": task.outcome_code,
            "outcome_note": task.outcome_note,
            "completed_at": task.completed_at_utc.isoformat() if task.completed_at_utc else None,
            "latest_call": {
                "state": latest_call.state,
                "duration_sec": latest_call.duration_sec,
                "created_at": latest_call.created_at_utc.isoformat()
            } if latest_call else None,
            "call_history": call_history,
            "sms_sent": task.last_sms_sent_at is not None
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting task: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/tasks")
async def search_tasks(
    status: Optional[str] = Query(None),
    workflow_type: Optional[str] = Query(None),
    therapist_phone: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Search tasks with pagination"""
    try:
        # Normalize phone if provided
        if therapist_phone:
            try:
                therapist_phone, _ = normalize_us_number(therapist_phone)
            except:
                pass  # Keep original if can't normalize
        
        task_repo = TaskRepository(session)
        offset = (page - 1) * per_page

        tasks = await task_repo.search_tasks(
            org_ids=auth.org_ids,
            status=status,
            workflow_type=workflow_type,
            therapist_phone=therapist_phone,
            limit=per_page,
            offset=offset
        )
        
        return {
            "tasks": [
                {
                    "task_id": str(task.id),
                    "status": task.status,
                    "workflow_type": task.workflow_type,
                    "patient_alias": task.patient_alias,
                    "doctor_name": task.doctor_name,
                    "doctor_phone": format_display(task.doctor_phone) if (hasattr(task, 'doctor_phone') and task.doctor_phone) else 'N/A',
                    "created_at": task.created_at_utc.isoformat(),
                    "outcome_code": task.outcome_code,
                    "attempts": task.attempts
                }
                for task in tasks
            ],
            "page": page,
            "per_page": per_page,
            "count": len(tasks)
        }
        
    except Exception as e:
        logger.error(f"Error searching tasks: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# PHI redaction helper
def redact_phone(phone: str) -> str:
    """Redact phone number for logging, keeping last 4 digits."""
    if not phone:
        return "***"
    digits = ''.join(filter(str.isdigit, phone))
    if len(digits) >= 4:
        return f"***{digits[-4:]}"
    return "***"

# Twilio signature validation helper
def validate_twilio_signature(request: Request, form_data: dict) -> bool:
    """
    Validate Twilio webhook signature.
    Returns True if valid, False if invalid.
    Skips validation if TWILIO_AUTH_TOKEN not set or SKIP_TWILIO_SIGNATURE_VALIDATION=true.

    SECURITY: SKIP_TWILIO_SIGNATURE_VALIDATION bypass only works when APP_ENV=development.
    In production, signature validation is always enforced.
    """
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    skip_validation = os.getenv("SKIP_TWILIO_SIGNATURE_VALIDATION", "false").lower() == "true"
    app_env = os.getenv("APP_ENV", "production").lower()

    if skip_validation:
        if app_env != "development":
            logger.warning("[Webhook] SKIP_TWILIO_SIGNATURE_VALIDATION ignored - only allowed in development")
        else:
            logger.debug("[Webhook] Signature validation skipped (SKIP_TWILIO_SIGNATURE_VALIDATION=true, APP_ENV=development)")
            return True

    if not auth_token:
        logger.warning("[Webhook] TWILIO_AUTH_TOKEN not set - skipping signature validation")
        return True

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        logger.warning("[Webhook] Missing X-Twilio-Signature header")
        return False

    # Build the full URL that Twilio signed
    url = str(request.url)

    validator = RequestValidator(auth_token)
    is_valid = validator.validate(url, form_data, signature)

    if not is_valid:
        logger.warning(f"[Webhook] Invalid Twilio signature for URL: {url}")

    return is_valid

@app.post("/webhooks/twilio/status")
async def twilio_status_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    """
    Handle Twilio/ElevenLabs call status webhooks
    Updates task status and sends SMS on completion
    """
    try:
        # Parse webhook data first (needed for signature validation)
        form_data = await request.form()
        form_dict = dict(form_data)

        # Validate Twilio signature
        if not validate_twilio_signature(request, form_dict):
            logger.warning("[Webhook] Rejected: invalid Twilio signature")
            raise HTTPException(status_code=403, detail="Invalid signature")

        # Extract fields from validated form data
        call_sid = form_dict.get("CallSid") or form_dict.get("call_id")
        call_status = form_dict.get("CallStatus") or form_dict.get("status", "")
        call_duration = form_dict.get("CallDuration") or form_dict.get("duration", "0")
        
        if not call_sid:
            return {"error": "No call SID provided"}
        
        # Get repositories
        call_event_repo = CallEventRepository(session)
        task_repo = TaskRepository(session)
        sms_event_repo = SmsEventRepository(session)
        
        # Find call event (system/unscoped - validated via Twilio signature)
        call_event = await call_event_repo.get_by_twilio_sid_system(call_sid)
        if not call_event:
            logger.warning(f"[Webhook] Unknown call SID: {call_sid}")
            return {"error": "Unknown call SID"}
        
        task = call_event.task
        
        # Update call event (system/unscoped - validated via Twilio signature)
        duration_sec = int(call_duration) if call_duration.isdigit() else 0
        await call_event_repo.update_call_event_system(
            call_sid,
            call_status.lower(),
            duration_sec,
            form_dict
        )
        
        # Map call status to task status
        status_map = {
            "completed": "RESOLVED",
            "answered": "RESOLVED",
            "busy": "FAILED",
            "failed": "FAILED",
            "no-answer": "NO_ANSWER_RETRY"
        }
        
        new_status = status_map.get(call_status.lower())
        if not new_status:
            logger.warning(f"[Webhook] Unknown call status: {call_status}")
            return {"status": "ok"}
        
        # Handle based on status
        if new_status == "NO_ANSWER_RETRY":
            # Check retry limits
            max_attempts = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
            if task.attempts < max_attempts - 1:
                # Schedule retry using business hours logic
                next_retry = schedule_retry(task.created_at_utc, task.attempts + 1)
                await task_repo.increment_attempts_system(task.id, next_retry)
                await task_repo.update_status_system(task.id, "NO_ANSWER_RETRY")
                
                log_scheduling_decision(
                    str(task.id), 
                    datetime.now(timezone.utc), 
                    next_retry, 
                    f"Retry #{task.attempts + 1} scheduled"
                )
                logger.info(f"[Webhook] Task {task.id} scheduled for retry #{task.attempts + 1} at {next_retry}")
            else:
                # Max retries reached
                await task_repo.update_status_system(task.id, "FAILED", "Max retry attempts reached")
                new_status = "FAILED"
        else:
            # Terminal status
            await task_repo.update_status_system(task.id, new_status)

        # Log call completion activity
        activity_repo = ActivityLogRepository(session)
        event_type_map = {
            "RESOLVED": "call.completed",
            "FAILED": "call.failed",
            "NO_ANSWER_RETRY": "call.no_answer"
        }
        await activity_repo.log_event(
            org_id=task.org_id,
            event_type=event_type_map.get(new_status, "call.status_changed"),
            summary=f"Call {new_status.lower().replace('_', ' ')} for {task.doctor_name}",
            task_id=task.id,
            actor_id=None,  # System event
            details={"call_status": call_status, "duration_sec": duration_sec, "previous_attempts": task.attempts}
        )

        # Send SMS if terminal status
        if new_status in ["RESOLVED", "FAILED"]:
            # Check SMS rate limit
            max_per_hour = int(os.getenv("SMS_MAX_PER_HOUR", "3"))
            recent_count = await task_repo.count_recent_sms_for_therapist(task.therapist_phone)
            
            if recent_count >= max_per_hour:
                logger.warning(f"[SMS] Rate limit hit for phone {redact_phone(task.therapist_phone)}")
                await task_repo.update_status_system(task.id, task.status, f"{task.notes} | SMS rate limit exceeded")
            else:
                # Compose SMS
                brand = os.getenv("SMS_BRAND", "[ClinicWire]")
                portal_url = os.getenv("PORTAL_BASE_URL", "")
                
                outcome = "Completed" if new_status == "RESOLVED" else "Failed"
                message = f"{brand} Task #{str(task.id)[:8]} • {task.workflow_type} • Outcome: {outcome}"
                
                if portal_url:
                    message += f" • Link: {portal_url}/t/{task.id}"
                
                # Try to send SMS (deduped by unique constraint)
                sms_event = await sms_event_repo.create_sms_event(
                    task.id,
                    task.therapist_phone,
                    message,
                    'status_final'
                )
                
                if sms_event:
                    # Actually send via Twilio
                    if os.getenv("SIMULATE", "true").lower() != "true":
                        result = twilio_service.send_sms(task.therapist_phone, message)
                        if result and result.get('sid'):
                            await sms_event_repo.update_sms_status(result['sid'], task.org_id, 'SENT')
                    
                    await task_repo.update_sms_sent_system(task.id)
                    logger.info(f"[SMS] Sent to phone {redact_phone(task.therapist_phone)} for task {task.id}")

                    # Log SMS sent activity
                    await activity_repo.log_event(
                        org_id=task.org_id,
                        event_type="sms.sent",
                        summary=f"SMS notification sent for task outcome: {outcome}",
                        task_id=task.id,
                        actor_id=None,  # System event
                        details={"outcome": outcome}
                    )
                else:
                    logger.info(f"[SMS] Already sent for task {task.id}")
        
        return {"status": "ok", "task_id": str(task.id)}
        
    except HTTPException:
        # Re-raise HTTP exceptions (like 403 for invalid signature)
        raise
    except Exception as e:
        logger.error(f"Error in webhook: {e}")
        return {"error": "Internal error"}

@app.post("/webhooks/elevenlabs")
async def elevenlabs_webhook(request: Request):
    """Stub for future ElevenLabs webhooks"""
    data = await request.json()
    # Log only event type and call_id, not full payload (may contain PHI)
    event_type = data.get("type", "unknown")
    call_id = data.get("call_id", "unknown")
    logger.info(f"[ElevenLabs] Webhook received: type={event_type}, call_id={call_id}")
    return {"status": "ok"}


# ========================================
# Call Summary Webhook
# ========================================

# Outcome codes allowlist (v1)
VALID_OUTCOME_CODES = {
    # Primary outcomes (new taxonomy)
    "SIGNED_CONFIRMED",
    "RECEIVED_AWAITING_SIGNATURE",
    "NEEDS_RESEND",
    "WRONG_FAX_NUMBER",
    "WRONG_CONTACT",
    "CALLBACK_REQUESTED",
    "REFUSED",
    "VOICEMAIL_LEFT",
    "NO_ANSWER",
    "FAILED_TECHNICAL",
    # Legacy codes (accepted for backward compat with seed data)
    "CONFIRMED_RECEIVED",
    "CONFIRMED_SIGNED",
    "SIGNATURE_PENDING",
    "REFUSED_INFO",
    "NO_DECISION",
    "ERROR"
}

# Max allowed age for webhook timestamp (seconds)
WEBHOOK_TIMESTAMP_MAX_AGE = 300

# Max length for summary/next_step fields
SUMMARY_MAX_LENGTH = 500


def validate_call_summary_signature(request: Request, body: bytes) -> bool:
    """
    Validate call summary webhook signature using HMAC-SHA256.

    Supports TWO formats:
    1. ElevenLabs native: ElevenLabs-Signature: t=timestamp,v0=hash
       Signature = HMAC-SHA256(secret, timestamp + "." + body)
    2. Custom format: X-Webhook-Timestamp + X-Webhook-Signature: sha256=hash
       Signature = HMAC-SHA256(secret, timestamp + body)
    """
    import hmac
    import hashlib
    import re

    secret = os.getenv("CALL_SUMMARY_WEBHOOK_SECRET")
    if not secret:
        logger.warning("[CallSummary] CALL_SUMMARY_WEBHOOK_SECRET not set - rejecting request")
        return False

    # Check for ElevenLabs native format first
    elevenlabs_sig = request.headers.get("ElevenLabs-Signature", "")
    if elevenlabs_sig:
        # Parse format: t=timestamp,v0=hash
        match = re.match(r't=(\d+),v0=([a-f0-9]+)', elevenlabs_sig)
        if not match:
            logger.warning(f"[CallSummary] Invalid ElevenLabs-Signature format: {elevenlabs_sig[:50]}")
            return False

        timestamp = match.group(1)
        provided_hash = match.group(2)

        # Verify timestamp is recent
        try:
            ts_int = int(timestamp)
            now = int(datetime.now(timezone.utc).timestamp())
            if abs(now - ts_int) > WEBHOOK_TIMESTAMP_MAX_AGE:
                logger.warning(f"[CallSummary] ElevenLabs timestamp too old: {ts_int} (now: {now})")
                return False
        except ValueError:
            logger.warning(f"[CallSummary] Invalid ElevenLabs timestamp: {timestamp}")
            return False

        # ElevenLabs uses: HMAC(timestamp + "." + body)
        expected = hmac.new(
            secret.encode(),
            f"{timestamp}.".encode() + body,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(provided_hash, expected):
            logger.warning("[CallSummary] ElevenLabs signature mismatch")
            return False

        logger.debug("[CallSummary] ElevenLabs signature validated")
        return True

    # Fall back to custom format: X-Webhook-Timestamp + X-Webhook-Signature
    signature = request.headers.get("X-Webhook-Signature", "")
    timestamp = request.headers.get("X-Webhook-Timestamp", "")

    if not signature.startswith("sha256="):
        logger.warning("[CallSummary] Invalid signature format - must start with 'sha256='")
        return False

    if not timestamp:
        logger.warning("[CallSummary] Missing X-Webhook-Timestamp header")
        return False

    # Verify timestamp is recent (within 300 seconds)
    try:
        ts_int = int(timestamp)
        now = int(datetime.now(timezone.utc).timestamp())
        if abs(now - ts_int) > WEBHOOK_TIMESTAMP_MAX_AGE:
            logger.warning(f"[CallSummary] Timestamp too old: {ts_int} (now: {now})")
            return False
    except ValueError:
        logger.warning(f"[CallSummary] Invalid timestamp format: {timestamp}")
        return False

    # Compute expected signature: HMAC(secret, timestamp + body)
    expected = hmac.new(
        secret.encode(),
        (timestamp.encode() + body),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature[7:], expected):
        logger.warning("[CallSummary] Signature mismatch")
        return False

    logger.debug("[CallSummary] Custom signature validated")
    return True


def mask_phi_in_text(text: str) -> str:
    """
    Mask phone numbers and emails in text for PHI safety.
    """
    import re

    if not text:
        return text

    # Mask phone numbers (various formats)
    # Matches: +1234567890, (123) 456-7890, 123-456-7890, 123.456.7890, etc.
    phone_pattern = r'(\+?1?[-.\s]?)?\(?(\d{3})\)?[-.\s]?(\d{3})[-.\s]?(\d{4})'
    text = re.sub(phone_pattern, r'***-***-\4', text)

    # Mask emails
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    text = re.sub(email_pattern, '***@***.***', text)

    return text


class CallSummaryRequest(BaseModel):
    """Request model for call summary webhook"""
    call_sid: str = Field(..., min_length=1, description="Twilio Call SID")
    conversation_id: Optional[str] = Field(None, description="ElevenLabs conversation ID")
    outcome_code: str = Field(..., min_length=1, description="Structured outcome code")
    summary: str = Field(..., min_length=1, max_length=SUMMARY_MAX_LENGTH, description="Human-readable summary")
    next_step: Optional[str] = Field(None, max_length=SUMMARY_MAX_LENGTH, description="Recommended next action")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0, description="Confidence score 0.0-1.0")
    source: Optional[str] = Field(None, description="Source: elevenlabs, twilio_ci, manual")


@app.post("/webhooks/call-summary")
async def call_summary_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    """
    Receive call summary from ElevenLabs, Twilio CI, or manual sources.

    Required headers:
        X-Webhook-Timestamp: Unix timestamp (seconds)
        X-Webhook-Signature: sha256=<HMAC-SHA256(secret, timestamp + body)>

    Required fields: call_sid, outcome_code, summary
    Optional fields: conversation_id, next_step, confidence, source

    Security:
        - HMAC signature validation with replay protection
        - org_id derived from call_sid -> call_event -> task (never trusted from payload)
        - Idempotent: duplicate call_sid returns 200 without re-processing
        - PHI masked in stored summary/next_step
    """
    try:
        # Read raw body for signature validation
        body = await request.body()

        # Validate signature
        if not validate_call_summary_signature(request, body):
            raise HTTPException(status_code=403, detail="Invalid signature")

        # Parse and validate payload
        import json
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        # Validate required fields
        call_sid = data.get("call_sid")
        outcome_code = data.get("outcome_code")
        summary = data.get("summary")

        if not call_sid:
            raise HTTPException(status_code=400, detail="Missing required field: call_sid")
        if not outcome_code:
            raise HTTPException(status_code=400, detail="Missing required field: outcome_code")
        if not summary:
            raise HTTPException(status_code=400, detail="Missing required field: summary")

        # Validate outcome_code against allowlist
        if outcome_code not in VALID_OUTCOME_CODES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid outcome_code: {outcome_code}. Must be one of: {', '.join(sorted(VALID_OUTCOME_CODES))}"
            )

        # Enforce max length
        if len(summary) > SUMMARY_MAX_LENGTH:
            summary = summary[:SUMMARY_MAX_LENGTH]

        next_step = data.get("next_step")
        if next_step and len(next_step) > SUMMARY_MAX_LENGTH:
            next_step = next_step[:SUMMARY_MAX_LENGTH]

        # Look up call event by twilio_sid (system-scoped)
        call_event_repo = CallEventRepository(session)
        call_event = await call_event_repo.get_by_twilio_sid_system(call_sid)

        if not call_event:
            logger.warning(f"[CallSummary] Call event not found for call_sid: {call_sid}")
            raise HTTPException(status_code=404, detail="Call event not found")

        task = call_event.task
        if not task:
            logger.error(f"[CallSummary] Task not found for call_event: {call_event.id}")
            raise HTTPException(status_code=404, detail="Task not found")

        # Derive org_id from task (never trust from payload)
        org_id = task.org_id

        # Check for idempotency - has a call.summary event already been logged for this task?
        activity_repo = ActivityLogRepository(session)
        if await activity_repo.has_event_for_task(task.id, "call.summary"):
            logger.info(f"[CallSummary] Duplicate summary for task {task.id} - returning 200 idempotently")
            return {"status": "ok", "message": "Summary already processed", "task_id": str(task.id)}

        # Mask PHI in summary and next_step before storing
        masked_summary = mask_phi_in_text(summary)
        masked_next_step = mask_phi_in_text(next_step) if next_step else None

        # Build outcome_note
        if masked_next_step:
            outcome_note = f"{masked_summary}\n\nNext step: {masked_next_step}"
        else:
            outcome_note = masked_summary

        # Update task outcome
        task_repo = TaskRepository(session)
        await task_repo.update_outcome_v2(
            task_id=task.id,
            org_id=org_id,
            outcome_code=outcome_code,
            outcome_note=outcome_note,
            completed_at_utc=datetime.now(timezone.utc)
        )

        # Log activity
        await activity_repo.log_event(
            org_id=org_id,
            event_type="call.summary",
            summary=f"Call summary received: {outcome_code}",
            task_id=task.id,
            actor_id=None,  # System event
            details={
                "outcome_code": outcome_code,
                "summary": masked_summary,
                "next_step": masked_next_step,
                "confidence": data.get("confidence"),
                "source": data.get("source"),
                "conversation_id": data.get("conversation_id")
            }
        )

        logger.info(f"[CallSummary] Processed summary for task {task.id}: outcome={outcome_code}")

        return {
            "status": "ok",
            "task_id": str(task.id),
            "outcome_code": outcome_code
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CallSummary] Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail="Internal error")

@app.post("/tasks/{task_id}/call")
async def execute_call(
    task_id: str,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """
    Manually execute a call for a specific task
    This overrides automatic scheduling and calls immediately
    """
    try:
        task_repo = TaskRepository(session)
        call_event_repo = CallEventRepository(session)

        # Get task by ID (scoped to user's orgs)
        task = await task_repo.get_by_id(uuid.UUID(task_id), auth.org_ids)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # Check if already calling
        if task.status == 'CALLING':
            raise HTTPException(status_code=400, detail="Task is already calling")

        # Check if already resolved
        if task.status in ['RESOLVED', 'FAILED']:
            raise HTTPException(status_code=400, detail=f"Task already completed with status: {task.status}")

        # Execute call immediately
        if os.getenv("SIMULATE", "true").lower() == "true":
            # Create simulated call event
            await call_event_repo.create_call_event(
                task.id,
                task.org_id,
                'initiated',
                f"sim_manual_{task.id}"
            )
            await task_repo.update_status(task.id, task.org_id, 'CALLING')

            # Log activity
            activity_repo = ActivityLogRepository(session)
            await activity_repo.log_event(
                org_id=task.org_id,
                event_type="call.initiated",
                summary=f"Call initiated to {task.doctor_name} (simulation)",
                task_id=task.id,
                actor_id=auth.user_id,
                details={"mode": "simulation"}
            )

            # Schedule simulation
            asyncio.create_task(simulate_call_flow(task.id))

            return {"message": "Call initiated (simulation mode)", "task_id": str(task.id)}
        else:
            # Real call via ElevenLabs
            call_result = elevenlabs_service.make_call(
                to_number=task.doctor_phone,
                patient_name=task.patient_alias,
                doctor_name=task.doctor_name,
                date_sent=datetime.now().strftime("%m/%d/%Y"),
                fax_number=""
            )

            if call_result and "call_id" in call_result:
                # Store Twilio SID for webhook lookups
                twilio_sid = call_result.get("twilio_sid") or call_result["call_id"]
                await call_event_repo.create_call_event(
                    task.id,
                    task.org_id,
                    'initiated',
                    twilio_sid
                )
                await task_repo.update_status(task.id, task.org_id, 'CALLING')

                # Log activity
                activity_repo = ActivityLogRepository(session)
                await activity_repo.log_event(
                    org_id=task.org_id,
                    event_type="call.initiated",
                    summary=f"Call initiated to {task.doctor_name}",
                    task_id=task.id,
                    actor_id=auth.user_id,
                    details={"call_id": call_result["call_id"], "conversation_id": call_result["call_id"]}
                )

                # Start background poller to close the lifecycle
                asyncio.create_task(
                    poll_elevenlabs_conversation(task.id, call_result["call_id"], twilio_sid)
                )

                return {"message": "Call initiated successfully", "task_id": str(task.id), "call_id": call_result["call_id"]}
            else:
                raise HTTPException(status_code=500, detail="Failed to initiate call")
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error executing manual call: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Delete a task and all related events"""
    try:
        task_repo = TaskRepository(session)
        task = await task_repo.get_by_id(uuid.UUID(task_id), auth.org_ids)

        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        
        # Delete task (cascades to call_events and sms_events)
        await session.delete(task)
        await session.commit()
        
        logger.info(f"[Delete] Task {task_id} deleted")
        return {"message": "Task deleted successfully", "task_id": task_id}
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting task: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

class BulkDeleteRequest(BaseModel):
    task_ids: List[str]

@app.delete("/tasks/bulk")
async def delete_multiple_tasks(
    request: BulkDeleteRequest,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """Delete multiple tasks"""
    try:
        task_repo = TaskRepository(session)
        deleted_count = 0
        
        for task_id in request.task_ids:
            try:
                task = await task_repo.get_by_id(uuid.UUID(task_id))
                if task:
                    await session.delete(task)
                    deleted_count += 1
            except Exception as e:
                logger.warning(f"Failed to delete task {task_id}: {e}")
                continue
        
        await session.commit()
        
        logger.info(f"[Delete] Deleted {deleted_count} tasks")
        return {"message": f"Deleted {deleted_count} tasks", "deleted_count": deleted_count}
        
    except Exception as e:
        logger.error(f"Error deleting multiple tasks: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/health")
async def health_check(session: AsyncSession = Depends(get_session)):
    """Health check with database connectivity"""
    try:
        # Check database
        from sqlalchemy import text
        await session.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "version": "2.0.0",
        "services": {
            "db": db_status,
            "twilio": "ok" if twilio_service.is_configured() else "not configured",
            "elevenlabs": "ok" if elevenlabs_service.is_configured() else "not configured"
        },
        "environment": {
            "simulate": os.getenv("SIMULATE", "true"),
            "sms_brand": os.getenv("SMS_BRAND", "[ClinicWire]"),
            "portal_url": os.getenv("PORTAL_BASE_URL", "not set")
        }
    }

# Background tasks
async def check_stuck_calls(session: AsyncSession, task_repo: TaskRepository):
    """
    Watchdog: Check for calls stuck in CALLING state beyond MAX_HOLD_SECONDS
    Only runs if ENABLE_WATCHDOG=true
    """
    max_seconds = int(os.getenv("MAX_HOLD_SECONDS", "1200"))  # Default 20 minutes
    enable_outcome_v2 = os.getenv("ENABLE_OUTCOME_V2", "false").lower() == "true"
    
    stale_threshold = datetime.now(timezone.utc) - timedelta(seconds=max_seconds)
    
    # Find tasks stuck in CALLING state
    from sqlalchemy import select, and_
    from db.models_v2 import Task
    
    result = await session.execute(
        select(Task).where(
            and_(
                Task.status == 'CALLING',
                Task.updated_at_utc < stale_threshold
            )
        )
    )
    stuck_tasks = list(result.scalars().all())
    
    if stuck_tasks:
        logger.warning(f"[Watchdog] Found {len(stuck_tasks)} stuck calls")
        
        for task in stuck_tasks:
            logger.info(f"[Watchdog] Processing stuck task {task.id} (in CALLING for >{max_seconds}s)")
            
            # Check daily attempts to determine next status
            business_day = get_business_day_key(datetime.now(timezone.utc))
            daily_attempts = await task_repo.count_daily_attempts_for_phone(
                task.doctor_phone, 
                business_day
            )
            
            # Determine status based on daily cap
            if daily_attempts < 3:
                new_status = 'NO_ANSWER_RETRY'
                outcome_code = 'CALL_BACK_REQUESTED' if enable_outcome_v2 else None
                outcome_note = f'Auto-ended after {max_seconds}s on hold; scheduling callback' if enable_outcome_v2 else None
                
                # Schedule next retry
                next_retry = next_business_time(datetime.now(timezone.utc))
                await task_repo.increment_attempts(task.id, next_retry)
            else:
                new_status = 'FAILED'
                outcome_code = 'TIMEOUT' if enable_outcome_v2 else None
                outcome_note = f'Auto-ended after {max_seconds}s; daily attempt limit reached' if enable_outcome_v2 else None
            
            # Update task status
            await task_repo.update_status(task.id, new_status)
            
            # Update outcome fields if v2 enabled
            if enable_outcome_v2:
                completed_at = datetime.now(timezone.utc) if new_status == 'FAILED' else None
                await task_repo.update_outcome_v2(
                    task.id,
                    outcome_code=outcome_code,
                    outcome_note=outcome_note,
                    completed_at_utc=completed_at
                )
            
            logger.info(f"[Watchdog] Updated task {task.id} to {new_status}")

async def process_retries():
    """Background task to process retries"""
    logger.info("[Retry] Background retry processor started")
    
    while True:
        try:
            # Check every minute
            await asyncio.sleep(60)
            
            async for session in get_session():
                task_repo = TaskRepository(session)
                call_event_repo = CallEventRepository(session)
                
                # Watchdog: Check for stuck calls if enabled
                if os.getenv("ENABLE_WATCHDOG", "false").lower() == "true":
                    await check_stuck_calls(session, task_repo)
                
                # Get tasks needing retry
                tasks = await task_repo.get_tasks_for_retry()
                
                for task in tasks:
                    # Check if we can call now (business hours + daily limits)
                    now = datetime.now(timezone.utc)
                    
                    # Get daily attempts for this doctor's phone number
                    business_day = get_business_day_key(now)
                    daily_attempts = await task_repo.count_daily_attempts_for_phone(task.doctor_phone, business_day)
                    
                    can_call, reason = validate_call_timing(now, task.doctor_phone, daily_attempts)
                    
                    if not can_call:
                        # Reschedule for next business slot
                        next_slot = next_business_time(now)
                        await task_repo.increment_attempts(task.id, next_slot)
                        log_scheduling_decision(
                            str(task.id), 
                            now, 
                            next_slot, 
                            f"Retry delayed: {reason}"
                        )
                        logger.info(f"[Retry] Task {task.id} rescheduled to {next_slot} ({reason})")
                        continue
                    
                    logger.info(f"[Retry] Processing retry for task {task.id}")
                    
                    if os.getenv("SIMULATE", "true").lower() == "true":
                        # Simulated retry
                        await call_event_repo.create_call_event(
                            task.id,
                            task.org_id,
                            'initiated',
                            f"sim_retry_{task.id}_{task.attempts}"
                        )
                        await task_repo.update_status(task.id, 'CALLING')
                        asyncio.create_task(simulate_call_flow(task.id))
                    else:
                        # Real retry
                        webhook_url = f"{os.getenv('MCP_BASE_URL')}/webhooks/twilio/status"
                        
                        call_result = elevenlabs_service.make_call(
                            to_number=task.doctor_phone,  # Call the doctor's phone
                            patient_name=task.patient_alias,
                            doctor_name=task.doctor_name,
                            date_sent=datetime.now().strftime("%m/%d/%Y"),
                            fax_number="",
                            webhook_url=webhook_url
                        )
                        
                        if call_result and "call_id" in call_result:
                            # Store Twilio SID for webhook lookups
                            twilio_sid = call_result.get("twilio_sid") or call_result["call_id"]
                            await call_event_repo.create_call_event(
                                task.id,
                                task.org_id,
                                'initiated',
                                twilio_sid
                            )
                            await task_repo.update_status(task.id, 'CALLING')
                
                break  # Exit the async for loop
                
        except Exception as e:
            logger.error(f"[Retry] Error in retry processor: {e}")

def _build_fallback_summary(
    outcome_code: str,
    outcome_flags: list,
    duration_sec: int,
    user_turns: list,
) -> str:
    """
    Build an operator-grade summary when ElevenLabs' transcript_summary
    is unavailable. Uses the already-inferred outcome, flags, and
    the most substantive user speech turn to produce a summary that
    answers: what happened, POC state, commitments, and follow-up.
    """
    n_turns = len(user_turns)
    has_time = "TIME_COMMITMENT_GIVEN" in outcome_flags
    has_followup = "FOLLOW_UP_NEEDED" in outcome_flags

    # Find the most substantive user turn (longest message) — this is
    # typically where the office states their answer.
    key_utterance = ""
    if user_turns:
        longest = max(user_turns, key=lambda t: len(t.get("message", "")))
        key_utterance = longest.get("message", "").strip()

    # Truncate to a reasonable display length
    if len(key_utterance) > 300:
        key_utterance = key_utterance[:297] + "..."

    call_info = f"({n_turns} exchanges, {duration_sec}s)"

    # Outcome-specific summary templates
    if outcome_code == "SIGNED_CONFIRMED":
        base = "Office confirmed the plan of care has been signed and returned."
    elif outcome_code == "RECEIVED_AWAITING_SIGNATURE":
        if has_time:
            base = "Office confirmed receipt of the plan of care and stated it would be signed and returned."
        else:
            base = "Office confirmed receipt of the plan of care and indicated intent to sign."
    elif outcome_code == "NEEDS_RESEND":
        base = "Office reported the fax was not received. Resend required."
    elif outcome_code == "REFUSED":
        base = "Office explicitly refused to sign or process the plan of care."
    elif outcome_code == "WRONG_CONTACT":
        base = "Contact is incorrect — person or office is not associated with this provider."
    elif outcome_code == "WRONG_FAX_NUMBER":
        base = "Office reported the fax number on file is incorrect."
    elif outcome_code == "CALLBACK_REQUESTED" and has_followup:
        base = f"Call connected {call_info} but outcome could not be determined from available data. Manual follow-up recommended."
    elif outcome_code == "CALLBACK_REQUESTED":
        base = "Office requested a callback at a later time."
    elif outcome_code == "VOICEMAIL_LEFT":
        base = "Reached voicemail. Message left regarding plan of care signature."
    elif outcome_code == "NO_ANSWER":
        base = f"No answer — call ended after {duration_sec}s with no verbal response."
    elif outcome_code == "FAILED_TECHNICAL":
        base = "Call failed due to a technical issue."
    else:
        base = f"Call completed {call_info}."

    # Append time commitment detail if present
    if has_time and outcome_code in ("RECEIVED_AWAITING_SIGNATURE", "SIGNED_CONFIRMED", "CALLBACK_REQUESTED"):
        # Extract time references from the key utterance — prefer specific over vague.
        lower_utt = key_utterance.lower()
        time_match = None
        # Check specific phrases first (longer = more informative)
        for phrase in ["before 5 p.m.", "before 4 p.m.", "before 3 p.m.",
                       "before 2 p.m.", "by the end of today",
                       "by the end of the day", "by end of day",
                       "by close of business", "before noon",
                       "by tomorrow morning", "by tomorrow",
                       "tomorrow morning", "tomorrow afternoon",
                       "this afternoon", "this morning",
                       "5 p.m.", "4 p.m.", "3 p.m.",
                       "before 5", "before 4", "before 3",
                       "end of today", "today", "tomorrow"]:
            if phrase in lower_utt:
                time_match = phrase
                break
        if time_match:
            base = base.rstrip(".") + f" — timeframe: {time_match}."

    # For substantive calls, append key quote if it adds value
    # (skip short pleasantries like "bye", "thank you", "okay")
    if key_utterance and len(key_utterance) > 40 and outcome_code not in ("NO_ANSWER", "FAILED_TECHNICAL"):
        base += f' Key response: "{key_utterance}"'

    return base


def _infer_outcome_code(
    status: str,
    call_successful: str,
    user_turns: list,
    termination_reason: str,
    raw_summary: str
) -> dict:
    """
    Infer an operator-grade outcome from ElevenLabs conversation data.

    Returns dict with:
      - "primary": one of the 10 primary outcome codes
      - "flags": list of secondary flags (0 or more)

    Priority order (first match wins for primary):
      1. Technical failures: call failed, no speech
      2. Wrong contact (always reliable, no ambiguity)
      3. Document state: signed → received/will sign → needs resend
         THIS OVERRIDES any secondary "declined" language.
      4. Explicit refusal to sign/process (only if no positive document signals)
      5. Logistics: wrong fax number, voicemail, callback requested
      6. Fallback: NO_ANSWER

    REFUSED hard rule: ONLY used when the summary explicitly indicates
    refusal to sign, refusal to process the POC, or refusal to provide info.
    "Declined callback", "declined resend", "already have it" are NOT refusals.

    Flags are additive — all matching flags are returned regardless of primary.
    """
    result = {"primary": None, "flags": []}

    # --- Technical failures (no summary to parse) ---
    if status == "failed":
        result["primary"] = "FAILED_TECHNICAL"
        return result

    if len(user_turns) == 0:
        result["primary"] = "NO_ANSWER"
        return result

    # If we have user turns but no summary text, do NOT return NO_ANSWER.
    # The call was answered. Fall through to keyword matching on whatever
    # text we have (may be transcript-derived). If nothing matches,
    # the end-of-function fallback handles answered-but-unclear calls.
    if not raw_summary.strip():
        # No text at all to analyze, but call WAS answered (user_turns > 0).
        # This should be rare — poller now passes transcript text as fallback.
        # Use CALLBACK_REQUESTED + FOLLOW_UP_NEEDED: we connected but can't
        # determine outcome, so operator should follow up.
        result["primary"] = "CALLBACK_REQUESTED"
        result["flags"].append("FOLLOW_UP_NEEDED")
        return result

    s = raw_summary.lower()

    # =================================================================
    # FLAGS: collected first, independent of primary outcome.
    # These are secondary actions, NOT outcomes.
    # =================================================================

    # RESEND_DECLINED: declined a resend offer (NOT a refusal to sign)
    if any(p in s for p in [
        "declined the resend", "declined a resend", "declined to have it resent",
        "declined resend", "did not need a resend", "did not want a resend",
        "doesn't need a resend", "does not need a resend",
        "no need to resend", "no resend needed", "resend was not necessary",
        "declined the offer to resend", "declined to receive another"
    ]):
        result["flags"].append("RESEND_DECLINED")

    # CALLBACK_DECLINED: declined an offer to call back (NOT a refusal)
    if any(p in s for p in [
        "declined the callback", "declined a callback", "declined to call back",
        "declined the offer to call back", "but the user declined",
        "but they declined", "did not need a callback",
        "no need to call back", "no callback needed",
        "declined the follow-up call", "declined further calls"
    ]):
        result["flags"].append("CALLBACK_DECLINED")

    # TIME_COMMITMENT_GIVEN: specific timeframe mentioned for action
    if any(p in s for p in [
        "before 5", "before 4", "before 3", "before 2", "before noon",
        "by end of day", "by the end of the day", "by close of business",
        "by tomorrow", "tomorrow", "by monday", "by tuesday",
        "by wednesday", "by thursday", "by friday",
        "this afternoon", "this morning",
        "within the hour", "within 24 hours", "today",
        "p.m.", "a.m."
    ]):
        result["flags"].append("TIME_COMMITMENT_GIVEN")

    if any(p in s for p in ["urgent", "urgency", "as soon as possible", "asap", "time-sensitive"]):
        result["flags"].append("URGENT_NOTED")

    if any(p in s for p in ["multiple fax", "multiple document", "several fax", "both fax"]):
        result["flags"].append("MULTIPLE_DOCS")

    if any(p in s for p in ["follow up", "follow-up", "following up", "check back"]):
        result["flags"].append("FOLLOW_UP_NEEDED")

    # =================================================================
    # PRIMARY OUTCOME: first match wins.
    # =================================================================

    # --- Layer 1: Wrong contact (always unambiguous) ---
    if any(p in s for p in [
        "wrong number", "wrong contact", "wrong office", "wrong person",
        "no longer at", "no longer here", "no longer with",
        "retired", "not at this location", "left the practice",
        "doesn't work here", "does not work here", "moved away",
        "no such person", "never heard of", "not a patient here"
    ]):
        result["primary"] = "WRONG_CONTACT"
        return result

    # --- Layer 2: Document state (OVERRIDES any declined language) ---
    # This is checked BEFORE refusal because: if they confirmed receipt
    # and/or stated intent to sign, that is the operative outcome.
    # Any "declined callback/resend" in the same summary is a secondary flag.

    has_signed = any(p in s for p in [
        "already signed", "signed the", "signed and fax", "signed and sent",
        "signature complete", "signed it", "has been signed"
    ])

    has_will_sign = any(p in s for p in [
        "will sign", "would sign", "going to sign", "plan to sign",
        "plans to sign", "intend to sign", "promised to sign",
        "agreed to sign", "will be signed", "get it signed",
        "get him to sign", "get her to sign", "get dr", "get doctor",
        "fax it back", "fax back", "faxed back", "send it back",
        "have it sent", "get it sent", "sent back",
        "return it", "will sign and fax back", "will sign and send back",
        "sign and fax it back", "sign and return"
    ])

    has_not_received = any(p in s for p in [
        "didn't receive", "did not receive", "not received",
        "never received", "hasn't received", "has not received",
        "haven't received", "can't find", "cannot find",
        "unable to locate", "don't have", "do not have"
    ])

    has_received = not has_not_received and any(p in s for p in [
        "confirmed receipt", "confirmed they received", "confirmed receiving",
        "received the fax", "received the plan", "received the document",
        "have the fax", "has the fax", "got the fax",
        "found the fax", "located the fax", "found it",
        "found the plan", "i found", "we found",
        "confirmed the fax", "confirmed the document", "confirmed the plan",
        "we have it", "they have it", "received it",
        "have the plan", "has the plan"
    ])

    # Already signed (past tense) → SIGNED_CONFIRMED
    if has_signed:
        result["primary"] = "SIGNED_CONFIRMED"
        return result

    # Confirmed receipt or stated intent to sign → RECEIVED_AWAITING_SIGNATURE
    if has_received or has_will_sign:
        result["primary"] = "RECEIVED_AWAITING_SIGNATURE"
        return result

    # Explicitly not received → NEEDS_RESEND
    if has_not_received:
        result["primary"] = "NEEDS_RESEND"
        return result

    # Resend request (but not if they declined a resend with no other context)
    if any(p in s for p in ["resend", "re-send", "send again", "send another"]):
        if "RESEND_DECLINED" not in result["flags"]:
            result["primary"] = "NEEDS_RESEND"
            return result

    # --- Layer 3: Explicit refusal to sign/process ---
    # ONLY fires when there are NO positive document signals above.
    # Must be specific phrases — bare "declined" or "refused" alone do NOT count.
    if any(p in s for p in [
        "refused to sign", "will not sign", "won't sign", "refuses to sign",
        "refused to participate", "declined to sign", "declined to provide",
        "refused to cooperate", "refused to confirm", "will not cooperate",
        "not handling this patient", "not our patient", "do not send",
        "stop calling", "do not call", "asked not to be called",
        "we are not handling", "will not process"
    ]):
        result["primary"] = "REFUSED"
        return result

    # --- Layer 4: Logistics ---
    if any(p in s for p in [
        "wrong fax number", "wrong fax", "incorrect fax number",
        "fax number is wrong", "fax number was wrong", "fax to the wrong"
    ]):
        result["primary"] = "WRONG_FAX_NUMBER"
        return result

    # Voicemail before callback — voicemail summaries often mention "callback"
    if any(p in s for p in ["voicemail", "voice mail", "left a message", "answering machine"]):
        result["primary"] = "VOICEMAIL_LEFT"
        return result

    if any(p in s for p in [
        "call back", "callback", "try again later", "call again",
        "better time", "not available right now", "busy right now"
    ]):
        result["primary"] = "CALLBACK_REQUESTED"
        return result

    # --- Layer 5: Weak positive ---
    if any(p in s for p in ["confirmed", "will take care of it", "acknowledged"]):
        result["primary"] = "RECEIVED_AWAITING_SIGNATURE"
        return result

    # Fallback: had speech but no clear keyword signal.
    # The call WAS answered (user_turns > 0 checked above), so NO_ANSWER
    # is categorically wrong here. Use CALLBACK_REQUESTED with FOLLOW_UP_NEEDED
    # to signal that an operator should verify the outcome manually.
    result["primary"] = "CALLBACK_REQUESTED"
    result["flags"].append("FOLLOW_UP_NEEDED")
    return result


async def poll_elevenlabs_conversation(task_id: uuid.UUID, conversation_id: str, twilio_sid: str):
    """
    Poll ElevenLabs for conversation status until it reaches a terminal state.
    When done/failed, write call.completed/call.failed and call.summary activity events.
    """
    POLL_INTERVAL = 5   # seconds between polls
    MAX_POLLS = 120     # 10 minutes max (120 * 5s)
    TERMINAL_STATUSES = {"done", "failed"}

    logger.info(f"[ElevenLabs-Poll] Starting for task={task_id}, conv={conversation_id}")

    for attempt in range(MAX_POLLS):
        await asyncio.sleep(POLL_INTERVAL)

        conv_data = elevenlabs_service.get_conversation(conversation_id)
        if not conv_data:
            logger.warning(f"[ElevenLabs-Poll] No data for conv={conversation_id}, attempt {attempt+1}")
            continue

        status = conv_data.get("status", "")
        logger.info(f"[ElevenLabs-Poll] conv={conversation_id} status={status} (attempt {attempt+1})")

        if status not in TERMINAL_STATUSES:
            continue

        # Terminal status reached - extract data
        metadata = conv_data.get("metadata") or {}
        analysis = conv_data.get("analysis") or {}
        transcript = conv_data.get("transcript") or []
        duration_sec = metadata.get("call_duration_secs") or 0
        termination_reason = metadata.get("termination_reason", "")
        call_successful = analysis.get("call_successful", "unknown")
        raw_summary = analysis.get("transcript_summary", "")

        # Count user speech turns to detect silent/no-response calls
        user_turns = [t for t in transcript if t.get("role") == "user" and t.get("message", "").strip()]

        # Determine if the ElevenLabs summary is usable
        # ElevenLabs returns error strings like "Unable to generate call summary..."
        # when their summarizer fails (common on silent/short calls)
        summary_is_error = (
            not raw_summary
            or "unable to generate" in raw_summary.lower()
            or "unexpected error" in raw_summary.lower()
        )

        # When ElevenLabs summary fails but we have transcript, build a
        # fallback summary from USER speech only. Agent speech introduces
        # false positives (e.g., agent asking "would you like me to resend?"
        # triggers NEEDS_RESEND even when user declined).
        inference_summary = ""
        if summary_is_error and len(user_turns) > 0:
            user_messages = [
                t.get("message", "").strip()
                for t in user_turns
                if t.get("message", "").strip()
            ]
            inference_summary = " ".join(user_messages)
            logger.info(
                f"[ElevenLabs-Poll] Summary unavailable, using user speech "
                f"({len(user_messages)} turns, {len(inference_summary)} chars) "
                f"for outcome inference"
            )
        elif not summary_is_error:
            inference_summary = raw_summary

        # Run outcome inference FIRST — summary text depends on outcome.
        outcome_result = _infer_outcome_code(
            status=status,
            call_successful=call_successful,
            user_turns=user_turns,
            termination_reason=termination_reason,
            raw_summary=inference_summary
        )
        outcome_code = outcome_result["primary"]
        outcome_flags = outcome_result["flags"]

        # Build human-readable summary text.
        if not summary_is_error:
            # ElevenLabs summary is good — use it directly.
            summary_text = raw_summary
        else:
            # ElevenLabs summary unavailable — build operator-grade summary
            # from outcome + flags + call metadata.
            summary_text = _build_fallback_summary(
                outcome_code=outcome_code,
                outcome_flags=outcome_flags,
                duration_sec=duration_sec,
                user_turns=user_turns,
            )

        # Mask PHI in summary
        masked_summary = mask_phi_in_text(summary_text)

        # Map to our call status
        if status == "done":
            call_status = "completed"
            new_task_status = "RESOLVED"
        else:
            call_status = "failed"
            new_task_status = "FAILED"

        logger.info(
            f"[ElevenLabs-Poll] Terminal: conv={conversation_id} "
            f"status={status} duration={duration_sec}s "
            f"successful={call_successful} reason={termination_reason} "
            f"user_turns={len(user_turns)} outcome={outcome_code}"
            f"{' flags=' + ','.join(outcome_flags) if outcome_flags else ''}"
        )

        # Write to database
        async with async_session_maker() as session:
            try:
                call_event_repo = CallEventRepository(session)
                task_repo = TaskRepository(session)
                activity_repo = ActivityLogRepository(session)

                # Update call event
                await call_event_repo.update_call_event_system(
                    twilio_sid,
                    call_status,
                    duration_sec,
                    {"elevenlabs_status": status, "termination_reason": termination_reason}
                )

                # Get task for org_id
                call_event = await call_event_repo.get_by_twilio_sid_system(twilio_sid)
                if not call_event or not call_event.task:
                    logger.error(f"[ElevenLabs-Poll] Task not found for twilio_sid={twilio_sid}")
                    return
                task = call_event.task

                # Update task status
                await task_repo.update_status_system(task.id, new_task_status)

                # Write call.completed / call.failed activity
                event_type = "call.completed" if call_status == "completed" else "call.failed"
                await activity_repo.log_event(
                    org_id=task.org_id,
                    event_type=event_type,
                    summary=f"Call {call_status} for {task.doctor_name}",
                    task_id=task.id,
                    actor_id=None,
                    details={
                        "call_status": call_status,
                        "duration_sec": duration_sec,
                        "termination_reason": termination_reason,
                        "previous_attempts": task.attempts,
                        "conversation_id": conversation_id
                    }
                )

                # Write call.summary activity
                await activity_repo.log_event(
                    org_id=task.org_id,
                    event_type="call.summary",
                    summary=f"Call summary received: {outcome_code}",
                    task_id=task.id,
                    actor_id=None,
                    details={
                        "outcome_code": outcome_code,
                        "flags": outcome_flags,
                        "summary": masked_summary,
                        "next_step": None,
                        "confidence": None,
                        "source": "elevenlabs-poll",
                        "conversation_id": conversation_id
                    }
                )

                # Update task outcome
                await task_repo.update_outcome_v2(
                    task_id=task.id,
                    org_id=task.org_id,
                    outcome_code=outcome_code,
                    outcome_note=masked_summary,
                    completed_at_utc=datetime.now(timezone.utc)
                )

                logger.info(
                    f"[ElevenLabs-Poll] Lifecycle closed: task={task.id} "
                    f"status={new_task_status} outcome={outcome_code}"
                )

            except Exception as e:
                logger.error(f"[ElevenLabs-Poll] DB error for conv={conversation_id}: {e}")

        return  # Done - exit the poller

    # Timed out
    logger.warning(f"[ElevenLabs-Poll] Timed out for conv={conversation_id} after {MAX_POLLS * POLL_INTERVAL}s")

    # Mark as failed on timeout
    async with async_session_maker() as session:
        try:
            call_event_repo = CallEventRepository(session)
            task_repo = TaskRepository(session)
            activity_repo = ActivityLogRepository(session)

            await call_event_repo.update_call_event_system(twilio_sid, "failed", 0, {"reason": "poll_timeout"})

            call_event = await call_event_repo.get_by_twilio_sid_system(twilio_sid)
            if call_event and call_event.task:
                task = call_event.task
                await task_repo.update_status_system(task.id, "FAILED", "ElevenLabs poll timeout")
                await activity_repo.log_event(
                    org_id=task.org_id,
                    event_type="call.failed",
                    summary=f"Call timed out for {task.doctor_name}",
                    task_id=task.id,
                    actor_id=None,
                    details={"call_status": "timeout", "conversation_id": conversation_id}
                )
        except Exception as e:
            logger.error(f"[ElevenLabs-Poll] Timeout cleanup error: {e}")


async def simulate_call_flow(task_id: str):
    """Simulate call completion for testing"""
    import random
    
    # Wait 3-5 seconds
    await asyncio.sleep(random.randint(3, 5))
    
    # Simulate webhook callback
    async for session in get_session():
        call_event_repo = CallEventRepository(session)
        
        # Get the call event
        result = await session.execute(
            f"SELECT * FROM call_events WHERE task_id = '{task_id}' ORDER BY created_at_utc DESC LIMIT 1"
        )
        call_event = result.first()
        
        if call_event:
            # Simulate success 80% of the time
            if random.random() < 0.8:
                status = "completed"
            else:
                status = "no-answer"
            
            # Trigger webhook internally
            form_data = {
                "CallSid": call_event.twilio_sid,
                "CallStatus": status,
                "CallDuration": "45" if status == "completed" else "0"
            }
            
            class MockRequest:
                async def form(self):
                    return form_data
            
            await twilio_status_webhook(MockRequest(), session)
        
        break

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)