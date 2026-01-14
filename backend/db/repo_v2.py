"""
Repository layer for production database operations
"""

from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone, date
import uuid
import hashlib
from sqlalchemy import select, update, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

from db.models_multitenant import Task, CallEvent, SmsEvent, Contact, ActivityLog

class TaskRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_or_get_task(self, data: Dict[str, Any]) -> Task:
        """Create a new task with idempotency check"""
        # Generate idempotency key based on patient+doctor+workflow+date
        created_date = date.today().isoformat()
        idempotency_key = self._generate_idempotency_key(
            patient_alias=data['patient_alias'],
            doctor_name=data['doctor_name'],
            doctor_phone=data['doctor_phone'],
            workflow_type=data.get('workflow_type', 'POC_SIGNATURE'),
            date_str=created_date
        )
        
        # Check if task already exists (scoped to org)
        existing = await self.get_by_idempotency_key(idempotency_key, data['org_id'])
        if existing:
            return existing
        
        # Create new task
        task = Task(
            org_id=data['org_id'],  # Add org_id
            workflow_type=data.get('workflow_type', 'POC_SIGNATURE'),
            patient_alias=data['patient_alias'],
            doctor_name=data['doctor_name'],
            doctor_phone=data['doctor_phone'],
            therapist_phone=data['therapist_phone'],
            idempotency_key=idempotency_key,
            status='QUEUED',
            attempts=0,
            notes=data.get('notes')
        )
        
        try:
            self.session.add(task)
            await self.session.commit()
            await self.session.refresh(task)
            return task
        except IntegrityError:
            # Race condition - another request created it
            await self.session.rollback()
            existing = await self.get_by_idempotency_key(idempotency_key, data['org_id'])
            if existing:
                return existing
            raise
    
    async def get_by_id(self, task_id: uuid.UUID, org_ids: List[uuid.UUID]) -> Optional[Task]:
        """Get task by ID with related events, scoped to org_ids"""
        result = await self.session.execute(
            select(Task)
            .options(selectinload(Task.call_events))
            .options(selectinload(Task.sms_events))
            .where(and_(Task.id == task_id, Task.org_id.in_(org_ids)))
        )
        return result.scalar_one_or_none()
    
    async def get_by_idempotency_key(self, key: str, org_id: uuid.UUID) -> Optional[Task]:
        """Get task by idempotency key, scoped to org_id"""
        result = await self.session.execute(
            select(Task).where(and_(Task.idempotency_key == key, Task.org_id == org_id))
        )
        return result.scalar_one_or_none()
    
    async def get_tasks_for_retry(self) -> List[Task]:
        """Get tasks that need retry processing"""
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(Task)
            .where(
                and_(
                    Task.status == 'NO_ANSWER_RETRY',
                    or_(
                        Task.next_retry_at.is_(None),
                        Task.next_retry_at <= now
                    )
                )
            )
            .order_by(Task.next_retry_at.nullslast())
        )
        return list(result.scalars().all())
    
    async def update_status(self, task_id: uuid.UUID, org_id: uuid.UUID, status: str, notes: Optional[str] = None) -> bool:
        """Update task status, scoped to org_id"""
        stmt = update(Task).where(and_(Task.id == task_id, Task.org_id == org_id)).values(
            status=status,
            notes=notes,
            updated_at_utc=datetime.now(timezone.utc)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0

    async def update_status_system(self, task_id: uuid.UUID, status: str, notes: Optional[str] = None) -> bool:
        """Update task status (unscoped - for system/webhook use only)"""
        stmt = update(Task).where(Task.id == task_id).values(
            status=status,
            notes=notes,
            updated_at_utc=datetime.now(timezone.utc)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0
    
    async def increment_attempts(self, task_id: uuid.UUID, org_id: uuid.UUID, next_retry_at: Optional[datetime] = None) -> bool:
        """Increment retry attempts and set next retry time, scoped to org_id"""
        stmt = update(Task).where(and_(Task.id == task_id, Task.org_id == org_id)).values(
            attempts=Task.attempts + 1,
            next_retry_at=next_retry_at,
            updated_at_utc=datetime.now(timezone.utc)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0

    async def increment_attempts_system(self, task_id: uuid.UUID, next_retry_at: Optional[datetime] = None) -> bool:
        """Increment retry attempts (unscoped - for system/webhook use only)"""
        stmt = update(Task).where(Task.id == task_id).values(
            attempts=Task.attempts + 1,
            next_retry_at=next_retry_at,
            updated_at_utc=datetime.now(timezone.utc)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0
    
    async def update_sms_sent(self, task_id: uuid.UUID, org_id: uuid.UUID) -> bool:
        """Update last SMS sent timestamp, scoped to org_id"""
        stmt = update(Task).where(and_(Task.id == task_id, Task.org_id == org_id)).values(
            last_sms_sent_at=datetime.now(timezone.utc)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0

    async def update_sms_sent_system(self, task_id: uuid.UUID) -> bool:
        """Update last SMS sent timestamp (unscoped - for system/webhook use only)"""
        stmt = update(Task).where(Task.id == task_id).values(
            last_sms_sent_at=datetime.now(timezone.utc)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0
    
    async def count_recent_sms_for_therapist(self, therapist_phone: str, hours: int = 1) -> int:
        """Count SMS sent to therapist in last N hours"""
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        result = await self.session.execute(
            select(func.count(Task.id))
            .where(
                and_(
                    Task.therapist_phone == therapist_phone,
                    Task.last_sms_sent_at >= since
                )
            )
        )
        return result.scalar() or 0
    
    async def count_daily_attempts_for_phone(self, therapist_phone: str, business_day: str) -> int:
        """Count total attempts made to a phone number on a specific business day"""
        # For now, count all attempts regardless of date - will be enhanced later
        # This is a placeholder for daily attempt tracking
        result = await self.session.execute(
            select(func.count(Task.id))
            .where(Task.therapist_phone == therapist_phone)
        )
        return result.scalar() or 0
    
    async def search_tasks(
        self,
        org_ids: List[uuid.UUID],
        status: Optional[str] = None,
        workflow_type: Optional[str] = None,
        therapist_phone: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Task]:
        """Search tasks with filters, scoped to org_ids"""
        query = select(Task).where(Task.org_id.in_(org_ids))

        if status:
            query = query.where(Task.status == status)
        if workflow_type:
            query = query.where(Task.workflow_type == workflow_type)
        if therapist_phone:
            query = query.where(Task.therapist_phone == therapist_phone)

        query = query.order_by(Task.created_at_utc.desc())
        query = query.limit(limit).offset(offset)

        result = await self.session.execute(query)
        return list(result.scalars().all())
    
    def _generate_idempotency_key(self, patient_alias: str, doctor_name: str, doctor_phone: str, workflow_type: str, date_str: str) -> str:
        """Generate SHA256 hash for idempotency"""
        key_data = f"{patient_alias.lower()}|{doctor_name.lower()}|{doctor_phone}|{workflow_type.lower()}|{date_str}"
        return hashlib.sha256(key_data.encode()).hexdigest()
    
    async def update_outcome_v2(
        self,
        task_id: uuid.UUID,
        org_id: uuid.UUID,
        outcome_code: Optional[str] = None,
        outcome_note: Optional[str] = None,
        completed_at_utc: Optional[datetime] = None
    ) -> bool:
        """Update outcome v2 fields (when ENABLE_OUTCOME_V2=true), scoped to org_id"""
        values = {}
        if outcome_code is not None:
            values['outcome_code'] = outcome_code
        if outcome_note is not None:
            values['outcome_note'] = outcome_note
        if completed_at_utc is not None:
            values['completed_at_utc'] = completed_at_utc

        if not values:
            return False

        stmt = update(Task).where(and_(Task.id == task_id, Task.org_id == org_id)).values(**values)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0


class CallEventRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_call_event(self, task_id: uuid.UUID, state: str, twilio_sid: Optional[str] = None) -> CallEvent:
        """Create a new call event"""
        event = CallEvent(
            task_id=task_id,
            state=state,
            twilio_sid=twilio_sid
        )
        self.session.add(event)
        await self.session.commit()
        await self.session.refresh(event)
        return event
    
    async def update_call_event(self, twilio_sid: str, org_id: uuid.UUID, state: str, duration_sec: Optional[int] = None, raw_status: Optional[dict] = None) -> bool:
        """Update call event by Twilio SID, scoped to org_id via task join"""
        # First verify the call event belongs to a task in the org
        call_event = await self.get_by_twilio_sid(twilio_sid, org_id)
        if not call_event:
            return False

        stmt = update(CallEvent).where(CallEvent.twilio_sid == twilio_sid).values(
            state=state,
            duration_sec=duration_sec,
            raw_status_json=raw_status
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0

    async def update_call_event_system(self, twilio_sid: str, state: str, duration_sec: Optional[int] = None, raw_status: Optional[dict] = None) -> bool:
        """Update call event by Twilio SID (unscoped - for system/webhook use only)"""
        stmt = update(CallEvent).where(CallEvent.twilio_sid == twilio_sid).values(
            state=state,
            duration_sec=duration_sec,
            raw_status_json=raw_status
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0
    
    async def get_by_twilio_sid(self, twilio_sid: str, org_id: uuid.UUID) -> Optional[CallEvent]:
        """Get call event by Twilio SID, scoped to org_id via task join"""
        result = await self.session.execute(
            select(CallEvent)
            .join(Task)
            .options(selectinload(CallEvent.task))
            .where(and_(CallEvent.twilio_sid == twilio_sid, Task.org_id == org_id))
        )
        return result.scalar_one_or_none()

    async def get_by_twilio_sid_system(self, twilio_sid: str) -> Optional[CallEvent]:
        """Get call event by Twilio SID (unscoped - for system/webhook use only)"""
        result = await self.session.execute(
            select(CallEvent)
            .options(selectinload(CallEvent.task))
            .where(CallEvent.twilio_sid == twilio_sid)
        )
        return result.scalar_one_or_none()


class SmsEventRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_sms_event(self, task_id: uuid.UUID, to_number: str, body: str, sms_type: str = 'status_final', provider_sid: Optional[str] = None) -> Optional[SmsEvent]:
        """Create SMS event with deduplication"""
        # Get task to retrieve org_id
        result = await self.session.execute(
            select(Task).where(Task.id == task_id)
        )
        task = result.scalar_one_or_none()
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        # Generate body hash
        body_hash = hashlib.sha256(body.encode()).hexdigest()
        
        # Create event
        event = SmsEvent(
            org_id=task.org_id,  # Add org_id from task
            task_id=task_id,
            to_number=to_number,
            body_hash=body_hash,
            provider_sid=provider_sid,
            status='SENT',
            type=sms_type
        )
        
        try:
            self.session.add(event)
            await self.session.commit()
            await self.session.refresh(event)
            return event
        except IntegrityError:
            # Duplicate SMS for this task/type - that's OK, just return None
            await self.session.rollback()
            return None
    
    async def update_sms_status(self, provider_sid: str, org_id: uuid.UUID, status: str) -> bool:
        """Update SMS status from provider callback, scoped to org_id"""
        stmt = update(SmsEvent).where(and_(SmsEvent.provider_sid == provider_sid, SmsEvent.org_id == org_id)).values(
            status=status
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0

class ContactRepository:
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def search_contacts(self, org_ids: List[uuid.UUID], q: Optional[str] = None, limit: int = 10) -> List["Contact"]:
        """Search contacts by doctor name or office name, scoped to org_ids"""
        from .models_multitenant import Contact
        query = select(Contact).where(Contact.org_id.in_(org_ids))

        if q:
            # Case-insensitive substring search
            search_term = f"%{q.lower()}%"
            query = query.where(
                or_(
                    func.lower(Contact.doctor_name).like(search_term),
                    func.lower(Contact.office_name).like(search_term)
                )
            )

        query = query.order_by(Contact.doctor_name, Contact.office_name).limit(limit)
        result = await self.session.execute(query)
        return list(result.scalars().all())
    
    async def get_by_id(self, contact_id: uuid.UUID, org_ids: List[uuid.UUID]) -> Optional["Contact"]:
        """Get contact by ID, scoped to org_ids"""
        from .models_multitenant import Contact
        query = select(Contact).where(and_(Contact.id == contact_id, Contact.org_id.in_(org_ids)))
        result = await self.session.execute(query)
        return result.scalar_one_or_none()
    
    async def create_contact(self, data: Dict[str, Any]) -> "Contact":
        """Create new contact"""
        from .models_multitenant import Contact
        contact = Contact(**data)
        self.session.add(contact)
        await self.session.commit()
        await self.session.refresh(contact)
        return contact
    
    async def update_contact(self, contact_id: uuid.UUID, org_ids: List[uuid.UUID], data: Dict[str, Any]) -> Optional["Contact"]:
        """Update existing contact, scoped to org_ids"""
        contact = await self.get_by_id(contact_id, org_ids)
        if not contact:
            return None

        # Update fields
        for key, value in data.items():
            if hasattr(contact, key):
                setattr(contact, key, value)

        # Update timestamp if phone/fax changed
        if 'phone_e164' in data or 'fax_e164' in data:
            contact.last_verified_at = datetime.now(timezone.utc)

        await self.session.commit()
        await self.session.refresh(contact)
        return contact
    
    async def verify_field(self, contact_id: uuid.UUID, org_ids: List[uuid.UUID], field: str) -> bool:
        """Update last_verified_at for a contact, scoped to org_ids"""
        from .models_multitenant import Contact
        if field not in ['phone', 'fax']:
            return False

        stmt = update(Contact).where(and_(Contact.id == contact_id, Contact.org_id.in_(org_ids))).values(
            last_verified_at=func.now()
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0


class ActivityLogRepository:
    """Repository for activity log operations"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def log_event(
        self,
        org_id: uuid.UUID,
        event_type: str,
        summary: str,
        task_id: Optional[uuid.UUID] = None,
        actor_id: Optional[uuid.UUID] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> ActivityLog:
        """Create a new activity log entry"""
        entry = ActivityLog(
            org_id=org_id,
            event_type=event_type,
            task_id=task_id,
            actor_id=actor_id,
            summary=summary,
            details=details
        )
        self.session.add(entry)
        await self.session.commit()
        await self.session.refresh(entry)
        return entry

    async def get_recent(
        self,
        org_ids: List[uuid.UUID],
        limit: int = 50,
        offset: int = 0,
        event_type: Optional[str] = None,
        task_id: Optional[uuid.UUID] = None
    ) -> List[ActivityLog]:
        """Get recent activity for organizations"""
        query = select(ActivityLog).where(ActivityLog.org_id.in_(org_ids))

        if event_type:
            query = query.where(ActivityLog.event_type == event_type)
        if task_id:
            query = query.where(ActivityLog.task_id == task_id)

        query = query.order_by(ActivityLog.created_at_utc.desc())
        query = query.limit(limit).offset(offset)

        result = await self.session.execute(query)
        return list(result.scalars().all())

    async def get_task_timeline(
        self,
        task_id: uuid.UUID,
        org_ids: List[uuid.UUID]
    ) -> List[ActivityLog]:
        """Get all activity for a specific task"""
        result = await self.session.execute(
            select(ActivityLog)
            .where(and_(ActivityLog.task_id == task_id, ActivityLog.org_id.in_(org_ids)))
            .order_by(ActivityLog.created_at_utc.asc())
        )
        return list(result.scalars().all())