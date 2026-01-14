"""
Activity API Router
Provides access to activity log for authenticated users
"""

import re
import uuid
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from db.database import get_session
from db.auth_session import get_auth_session
from db.models_multitenant import ActivityLog
from db.repo_v2 import ActivityLogRepository
from auth.jwt_handler import get_current_user, AuthContext

router = APIRouter(prefix="/api/activity", tags=["activity"])


# Phone number masking utility
def mask_phone(phone: Optional[str]) -> Optional[str]:
    """Mask phone number, keeping only last 4 digits"""
    if not phone:
        return None
    digits = re.sub(r'\D', '', phone)
    if len(digits) >= 4:
        return f"***-***-{digits[-4:]}"
    return "***"


def sanitize_details(details: Optional[dict]) -> Optional[dict]:
    """
    Sanitize details dict by masking phone numbers and removing sensitive data.
    """
    if not details:
        return None

    sanitized = {}
    sensitive_keys = {'raw_payload', 'raw_response', 'raw_status_json', 'auth_token', 'password', 'secret'}
    phone_keys = {'phone', 'to_number', 'from_number', 'therapist_phone', 'doctor_phone'}

    for key, value in details.items():
        # Skip sensitive keys
        if key.lower() in sensitive_keys:
            continue

        # Mask phone numbers
        if key.lower() in phone_keys and isinstance(value, str):
            sanitized[key] = mask_phone(value)
        elif isinstance(value, dict):
            # Recursively sanitize nested dicts
            sanitized[key] = sanitize_details(value)
        else:
            sanitized[key] = value

    return sanitized


# Response models
class ActivityEventResponse(BaseModel):
    id: str
    event_type: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    created_at_utc: datetime
    actor_id: Optional[str] = None
    summary_safe: str
    details: Optional[dict] = None

    class Config:
        from_attributes = True


class ActivityListResponse(BaseModel):
    items: List[ActivityEventResponse]
    next_cursor: Optional[str] = None
    has_more: bool = False


def activity_to_response(activity: ActivityLog) -> ActivityEventResponse:
    """Convert ActivityLog model to response format"""
    # Derive entity_type from event_type (e.g., "task.created" -> "task")
    entity_type = activity.event_type.split('.')[0] if '.' in activity.event_type else None

    # Entity ID is task_id for task/call/sms events
    entity_id = str(activity.task_id) if activity.task_id else None

    return ActivityEventResponse(
        id=str(activity.id),
        event_type=activity.event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        created_at_utc=activity.created_at_utc,
        actor_id=str(activity.actor_id) if activity.actor_id else None,
        summary_safe=activity.summary,
        details=sanitize_details(activity.details)
    )


@router.get("", response_model=ActivityListResponse)
async def list_activity(
    limit: int = Query(50, ge=1, le=200, description="Number of items to return"),
    cursor: Optional[str] = Query(None, description="Pagination cursor (activity ID to start after)"),
    event_type: Optional[str] = Query(None, description="Filter by event type (e.g., task.created)"),
    entity_type: Optional[str] = Query(None, description="Filter by entity type (task, call, sms)"),
    entity_id: Optional[str] = Query(None, description="Filter by entity ID (task UUID)"),
    since: Optional[datetime] = Query(None, description="Filter events after this timestamp"),
    until: Optional[datetime] = Query(None, description="Filter events before this timestamp"),
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """
    List activity events for the authenticated user's organizations.

    Events are returned newest-first. Use cursor for pagination.
    """
    # Build query
    query = select(ActivityLog).where(ActivityLog.org_id.in_(auth.org_ids))

    # Apply filters
    if event_type:
        query = query.where(ActivityLog.event_type == event_type)

    if entity_type:
        # Filter by event_type prefix (e.g., "task" matches "task.created", "task.deleted")
        query = query.where(ActivityLog.event_type.like(f"{entity_type}.%"))

    if entity_id:
        try:
            entity_uuid = uuid.UUID(entity_id)
            query = query.where(ActivityLog.task_id == entity_uuid)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid entity_id format")

    if since:
        query = query.where(ActivityLog.created_at_utc >= since)

    if until:
        query = query.where(ActivityLog.created_at_utc <= until)

    # Apply cursor (pagination)
    if cursor:
        try:
            cursor_uuid = uuid.UUID(cursor)
            # Get the cursor activity's timestamp
            cursor_result = await session.execute(
                select(ActivityLog.created_at_utc).where(ActivityLog.id == cursor_uuid)
            )
            cursor_time = cursor_result.scalar_one_or_none()
            if cursor_time:
                # Get items older than cursor
                query = query.where(
                    (ActivityLog.created_at_utc < cursor_time) |
                    ((ActivityLog.created_at_utc == cursor_time) & (ActivityLog.id < cursor_uuid))
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid cursor format")

    # Order and limit (fetch one extra to check if there's more)
    query = query.order_by(ActivityLog.created_at_utc.desc(), ActivityLog.id.desc())
    query = query.limit(limit + 1)

    result = await session.execute(query)
    activities = list(result.scalars().all())

    # Check if there are more items
    has_more = len(activities) > limit
    if has_more:
        activities = activities[:limit]

    # Build response
    items = [activity_to_response(a) for a in activities]

    # Set next cursor if there are more items
    next_cursor = None
    if has_more and items:
        next_cursor = items[-1].id

    return ActivityListResponse(
        items=items,
        next_cursor=next_cursor,
        has_more=has_more
    )


@router.get("/{activity_id}", response_model=ActivityEventResponse)
async def get_activity(
    activity_id: str,
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """
    Get a single activity event by ID.

    Only returns the event if it belongs to one of the user's organizations.
    """
    try:
        activity_uuid = uuid.UUID(activity_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid activity ID format")

    result = await session.execute(
        select(ActivityLog).where(
            and_(
                ActivityLog.id == activity_uuid,
                ActivityLog.org_id.in_(auth.org_ids)
            )
        )
    )
    activity = result.scalar_one_or_none()

    if not activity:
        raise HTTPException(status_code=404, detail="Activity not found")

    return activity_to_response(activity)
