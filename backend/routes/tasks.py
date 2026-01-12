"""
Task routes with PostgreSQL integration
"""

import os
import logging
from typing import Optional
from datetime import datetime, timezone
import uuid

from fastapi import APIRouter, HTTPException, Header, Depends, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from db import get_session, TaskRepository, CallEventRepository, SmsEventRepository
from services.twilio_service import TwilioService
from services.elevenlabs_service import ElevenLabsService
from sync.sheets import SheetsSyncService
from services.google_sheets_apps_script import GoogleSheetsService

logger = logging.getLogger(__name__)

# Initialize router
router = APIRouter(prefix="/tasks", tags=["tasks"])

# Initialize services
twilio_service = TwilioService()
elevenlabs_service = ElevenLabsService()
sheets_service = GoogleSheetsService()
sheets_sync = SheetsSyncService(sheets_service)

# Request/Response models
class TaskRequest(BaseModel):
    patient_name: str
    patient_dob: str
    doctor_name: str
    doctor_phone: str
    date_sent: str
    fax_number: str
    therapist_phone: str
    workflow_type: str = "poc_followup"

class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str

# Helper functions
def validate_auth_token(authorization: Optional[str] = Header(None)) -> bool:
    """Validate the authorization token"""
    if not authorization:
        return False
    
    expected_token = os.getenv("MCP_AUTH_TOKEN")
    if not expected_token:
        return True  # No auth configured, allow all
    
    try:
        scheme, token = authorization.split(" ")
        return scheme.lower() == "bearer" and token == expected_token
    except:
        return False

@router.post("", response_model=TaskResponse)
async def create_task(
    request: TaskRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    authorization: Optional[str] = Header(None)
):
    """
    Create a new POC calling task
    - Validates input
    - Creates task in database
    - Syncs to Google Sheet
    - Initiates call (or simulates if SIMULATE=true)
    """
    # Validate auth
    if not validate_auth_token(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    try:
        # Get repositories
        task_repo = TaskRepository(session)
        call_event_repo = CallEventRepository(session)
        
        # Prepare task data
        task_data = {
            'source': 'WEB',
            'workflow_type': request.workflow_type,
            'patient_name': request.patient_name,
            'doctor_name': request.doctor_name,
            'therapist_phone': request.therapist_phone,
            'notes': f"DOB: {request.patient_dob} | Sent: {request.date_sent} | Fax: {request.fax_number}"
        }
        
        # Create task in database
        task = await task_repo.create_task(task_data)
        logger.info(f"Created task {task.id} in database")
        
        # Schedule background sync to sheets
        background_tasks.add_task(
            sync_task_to_sheets,
            task.id
        )
        
        # Check if simulation mode
        if os.getenv("SIMULATE", "true").lower() == "true":
            # Create initial call event
            await call_event_repo.create_call_event(task.id, "INITIATED", f"sim_{task.id}")
            
            # Schedule simulated completion
            background_tasks.add_task(
                simulate_call_completion,
                task.id,
                request.therapist_phone,
                request.patient_name,
                request.doctor_name
            )
            logger.info(f"Task {task.id} scheduled for simulation")
        else:
            # Make real call via ElevenLabs
            webhook_url = f"{os.getenv('MCP_BASE_URL')}/webhooks/twilio/status"
            
            # Create call with ElevenLabs
            call_result = elevenlabs_service.make_call(
                to_number=request.doctor_phone,
                patient_name=request.patient_name,
                doctor_name=request.doctor_name,
                date_sent=request.date_sent,
                fax_number=request.fax_number,
                webhook_url=webhook_url
            )
            
            if call_result and "call_id" in call_result:
                # Create call event
                await call_event_repo.create_call_event(
                    task.id, 
                    "INITIATED", 
                    call_result["call_id"]
                )
                
                # Update task status
                await task_repo.update_status(task.id, "CALLING")
                
                logger.info(f"Call initiated for task {task.id}, call_id: {call_result['call_id']}")
        
        return TaskResponse(
            task_id=str(task.id),
            status="QUEUED",
            message="Task created successfully"
        )
        
    except Exception as e:
        logger.error(f"Error creating task: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("")
async def get_all_tasks(
    session: AsyncSession = Depends(get_session),
    limit: int = 100,
    offset: int = 0
):
    """Get all tasks with pagination"""
    try:
        from sqlalchemy import select
        from db.models import Task
        
        result = await session.execute(
            select(Task)
            .order_by(Task.created_at_utc.desc())
            .limit(limit)
            .offset(offset)
        )
        tasks = result.scalars().all()
        
        return {
            "success": True,
            "data": [
                {
                    "task_id": str(task.id),
                    "created_at": task.created_at_utc.isoformat(),
                    "status": task.status,
                    "patient_alias": task.patient_alias,
                    "doctor_name": task.doctor_name,
                    "workflow_type": task.workflow_type
                }
                for task in tasks
            ],
            "count": len(tasks),
            "limit": limit,
            "offset": offset
        }
    except Exception as e:
        logger.error(f"Error getting tasks: {e}")
        return {"success": False, "error": str(e)}

@router.get("/{task_id}")
async def get_task(
    task_id: str,
    session: AsyncSession = Depends(get_session)
):
    """Get a specific task by ID"""
    try:
        task_repo = TaskRepository(session)
        task = await task_repo.get_by_id(uuid.UUID(task_id))
        
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        
        return {
            "success": True,
            "data": {
                "task_id": str(task.id),
                "created_at": task.created_at_utc.isoformat(),
                "status": task.status,
                "patient_alias": task.patient_alias,
                "doctor_name": task.doctor_name,
                "workflow_type": task.workflow_type,
                "notes": task.notes,
                "call_events": [
                    {
                        "id": str(event.id),
                        "state": event.state,
                        "duration": event.duration_sec,
                        "created_at": event.created_at_utc.isoformat()
                    }
                    for event in task.call_events
                ],
                "sms_events": [
                    {
                        "id": str(event.id),
                        "to_number": event.to_number,
                        "status": event.status,
                        "created_at": event.created_at_utc.isoformat()
                    }
                    for event in task.sms_events
                ]
            }
        }
    except Exception as e:
        logger.error(f"Error getting task: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def sync_task_to_sheets(task_id: uuid.UUID):
    """Background task to sync a task to Google Sheets"""
    try:
        async for session in get_session():
            task_repo = TaskRepository(session)
            task = await task_repo.get_by_id(task_id)
            
            if task:
                # Convert to sheet format and sync
                sheet_data = await sheets_sync._db_to_sheet_format(task, session)
                sheets_service.append_row(sheet_data)
                
                # Update sync info
                await task_repo.update_sheet_sync(task_id, 0, datetime.now(timezone.utc))
                
                logger.info(f"Synced task {task_id} to Google Sheets")
    except Exception as e:
        logger.error(f"Error syncing task to sheets: {e}")

async def simulate_call_completion(task_id: uuid.UUID, therapist_phone: str, patient_name: str, doctor_name: str):
    """Simulate a successful call completion after 3-5 seconds"""
    import asyncio
    import random
    
    # Wait 3-5 seconds
    await asyncio.sleep(random.randint(3, 5))
    
    try:
        async for session in get_session():
            task_repo = TaskRepository(session)
            call_event_repo = CallEventRepository(session)
            sms_event_repo = SmsEventRepository(session)
            
            # Update call event
            await call_event_repo.update_call_status(
                f"sim_{task_id}",
                "COMPLETED",
                45,
                {"simulation": True}
            )
            
            # Update task status
            await task_repo.update_status(
                task_id,
                "RESOLVED",
                "(SIMULATED) Call completed successfully - POC signed and faxed"
            )
            
            # Create SMS event
            message = f"(SIM) POC Update: Call to {doctor_name}'s office for {patient_name} completed successfully. POC was signed and faxed."
            await sms_event_repo.create_sms_event(
                task_id,
                therapist_phone,
                message,
                f"sim_sms_{task_id}"
            )
            
            logger.info(f"Simulated completion for task {task_id}")