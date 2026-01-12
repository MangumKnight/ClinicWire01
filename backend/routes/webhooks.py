"""
Webhook routes for Twilio and ElevenLabs callbacks
"""

import os
import logging
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, Request, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_session, TaskRepository, CallEventRepository, SmsEventRepository
from services.twilio_service import TwilioService
from sync.sheets import SheetsSyncService
from services.google_sheets_apps_script import GoogleSheetsService

logger = logging.getLogger(__name__)

# Initialize router
router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Initialize services
twilio_service = TwilioService()
sheets_service = GoogleSheetsService()
sheets_sync = SheetsSyncService(sheets_service)

@router.post("/twilio/status")
async def twilio_status_callback(
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    """
    Handle Twilio/ElevenLabs status callbacks
    - Updates database
    - Syncs to Google Sheet
    - Sends SMS to therapist
    """
    # Get form data from callback
    form_data = await request.form()
    
    # Extract relevant fields
    call_sid = form_data.get("CallSid") or form_data.get("call_id")
    call_status = form_data.get("CallStatus") or form_data.get("status")
    call_duration = form_data.get("CallDuration") or form_data.get("duration")
    
    if not call_sid:
        logger.error("No call_sid in callback")
        return {"status": "error", "message": "No call_sid provided"}
    
    try:
        # Get repositories
        task_repo = TaskRepository(session)
        call_event_repo = CallEventRepository(session)
        sms_event_repo = SmsEventRepository(session)
        
        # Find call event
        call_event = await call_event_repo.get_by_twilio_sid(call_sid)
        if not call_event:
            # Try to find task by call_sid from sheets (legacy support)
            task = await task_repo.find_by_call_sid(call_sid)
            if not task:
                logger.error(f"No task found for call_sid: {call_sid}")
                return {"status": "error", "message": "Task not found"}
        else:
            # Get task from call event
            task = await task_repo.get_by_id(call_event.task_id)
        
        if not task:
            logger.error(f"Task not found for call event")
            return {"status": "error", "message": "Task not found"}
        
        # Map call status
        status_map = {
            "completed": "RESOLVED",
            "busy": "FAILED",
            "failed": "FAILED",
            "no-answer": "FAILED",
            "canceled": "FAILED"
        }
        
        new_status = status_map.get(call_status.lower(), "RESOLVED")
        duration_sec = int(call_duration) if call_duration else 0
        
        # Update call event
        await call_event_repo.update_call_status(
            call_sid,
            call_status.upper(),
            duration_sec,
            dict(form_data)  # Store all callback data
        )
        
        # Update task status
        outcome_note = "Call completed - POC signed" if new_status == "RESOLVED" else f"Call {call_status}"
        await task_repo.update_status(task.id, new_status, outcome_note)
        
        # Update outcome v2 fields if enabled
        if os.getenv("ENABLE_OUTCOME_V2", "false").lower() == "true":
            # Determine outcome code based on status
            outcome_code_map = {
                "completed": "POC_SIGNED",
                "busy": "LINE_BUSY",
                "failed": "CALL_FAILED",
                "no-answer": "NO_ANSWER",
                "canceled": "CALL_CANCELED"
            }
            outcome_code = outcome_code_map.get(call_status.lower(), "UNKNOWN")
            
            # Check for long calls that might indicate hold/voicemail
            if duration_sec and duration_sec > int(os.getenv("MAX_HOLD_SECONDS", "1200")):
                outcome_code = "LEFT_VOICEMAIL"
                outcome_note = f"Call lasted {duration_sec}s - likely voicemail or extended hold"
            
            # Set completed_at for terminal states
            completed_at = datetime.now(timezone.utc) if new_status in ["RESOLVED", "FAILED"] else None
            
            await task_repo.update_outcome_v2(
                task.id,
                outcome_code=outcome_code,
                outcome_note=outcome_note,
                completed_at_utc=completed_at
            )
        
        # Prepare SMS message
        if new_status == "RESOLVED":
            message = f"POC Update: Call to {task.doctor_name}'s office for {task.patient_alias} completed successfully. Duration: {duration_sec}s. Check system for details."
        else:
            message = f"POC Update: Call to {task.doctor_name}'s office for {task.patient_alias} failed ({call_status}). Please follow up manually."
        
        # Send SMS if not in simulation mode
        if task.therapist_phone and os.getenv("SIMULATE", "true").lower() != "true":
            sms_result = twilio_service.send_sms(task.therapist_phone, message)
            if sms_result:
                await sms_event_repo.create_sms_event(
                    task.id,
                    task.therapist_phone,
                    message,
                    sms_result.get("sid")
                )
        else:
            logger.info(f"(SIM) SMS to {task.therapist_phone}: {message}")
        
        # Sync to Google Sheets
        try:
            sheet_data = await sheets_sync._db_to_sheet_format(task, session)
            sheets_service.update_task_field(str(task.id), "status", new_status)
            sheets_service.update_task_field(str(task.id), "call_duration", str(duration_sec))
            sheets_service.update_task_field(str(task.id), "call_outcome", outcome_note)
            sheets_service.update_task_field(str(task.id), "last_updated", datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logger.error(f"Error syncing to sheets: {e}")
        
        logger.info(f"Updated task {task.id} with status {new_status}")
        return {"status": "success", "task_id": str(task.id)}
        
    except Exception as e:
        logger.error(f"Error processing callback: {e}")
        return {"status": "error", "message": str(e)}

@router.post("/elevenlabs")
async def elevenlabs_webhook(
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    """
    Handle ElevenLabs-specific webhooks
    """
    data = await request.json()
    logger.info(f"ElevenLabs webhook received: {data}")
    
    # Process based on webhook type
    webhook_type = data.get("type", "")
    
    if webhook_type == "call.started":
        # Handle call started event
        call_id = data.get("call_id")
        if call_id:
            # Update call event status
            call_event_repo = CallEventRepository(session)
            await call_event_repo.update_call_status(call_id, "RINGING")
    
    elif webhook_type == "call.completed":
        # Handle call completed event
        # This might duplicate the Twilio callback, so check first
        pass
    
    return {"status": "success"}

@router.post("/twilio/sms")
async def twilio_sms_callback(
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    """
    Handle Twilio SMS status callbacks
    """
    form_data = await request.form()
    
    message_sid = form_data.get("MessageSid")
    message_status = form_data.get("MessageStatus")
    
    if message_sid and message_status:
        sms_event_repo = SmsEventRepository(session)
        await sms_event_repo.update_sms_status(
            message_sid,
            message_status.upper()
        )
        logger.info(f"Updated SMS {message_sid} status to {message_status}")
    
    return {"status": "success"}