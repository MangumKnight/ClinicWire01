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
from db.database import get_session, close_database
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
app.include_router(contacts_router)
app.include_router(auth_router)
app.include_router(organizations_router)
app.include_router(activity_router)

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
        
        # Get latest call event
        latest_call = None
        if task.call_events:
            latest_call = max(task.call_events, key=lambda x: x.created_at_utc)
        
        return {
            "task_id": str(task.id),
            "status": task.status,
            "workflow_type": task.workflow_type,
            "patient_alias": task.patient_alias,
            "doctor_name": task.doctor_name,
            "therapist_phone": format_display(task.therapist_phone),
            "created_at": task.created_at_utc.isoformat(),
            "updated_at": task.updated_at_utc.isoformat(),
            "attempts": task.attempts,
            "latest_call": {
                "state": latest_call.state,
                "duration_sec": latest_call.duration_sec,
                "created_at": latest_call.created_at_utc.isoformat()
            } if latest_call else None,
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
                    "created_at": task.created_at_utc.isoformat()
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
    """
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    skip_validation = os.getenv("SKIP_TWILIO_SIGNATURE_VALIDATION", "false").lower() == "true"

    if skip_validation:
        logger.debug("[Webhook] Signature validation skipped (SKIP_TWILIO_SIGNATURE_VALIDATION=true)")
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
                    details={"call_id": call_result["call_id"]}
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
                                'initiated',
                                twilio_sid
                            )
                            await task_repo.update_status(task.id, 'CALLING')
                
                break  # Exit the async for loop
                
        except Exception as e:
            logger.error(f"[Retry] Error in retry processor: {e}")

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