"""
Dashboard API Router
Provides ROI and metrics data for authenticated users.

All endpoints require authentication and derive org_id from JWT token.
org_id is NEVER accepted from client - enforced by auth.org_ids filter.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, List

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_

from db.auth_session import get_auth_session
from db.models_multitenant import Task, CallEvent
from auth.jwt_handler import get_current_user, AuthContext

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# =============================================================================
# BACKEND CONSTANTS (single source of truth)
# =============================================================================

# Minutes saved per call - configurable via env, default 15
DEFAULT_MINUTES_SAVED_PER_CALL = int(os.getenv("DEFAULT_MINUTES_SAVED_PER_CALL", "15"))

# Hourly rate for cost savings - configurable via env, default $35
DEFAULT_HOURLY_RATE = int(os.getenv("DEFAULT_HOURLY_RATE", "35"))

# Success outcome codes - single source of truth
# All other outcome codes are considered non-success
SUCCESS_OUTCOME_CODES = ['CONFIRMED_RECEIVED', 'CONFIRMED_SIGNED', 'SIGNATURE_PENDING']

# Outcome code labels for display
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


# =============================================================================
# RESPONSE MODELS
# =============================================================================

class PeriodResponse(BaseModel):
    start: datetime
    end: datetime


class AssumptionsResponse(BaseModel):
    minutes_saved_per_call: int
    hourly_rate: int


class SummaryMetricsResponse(BaseModel):
    # Exact counts
    calls_attempted: int
    calls_connected: int
    successful_outcomes: int
    # Calculated (exact)
    avg_days_to_resolution: Optional[float]
    # Estimated
    estimated_hours_saved: float
    estimated_cost_saved: float


class DashboardSummaryResponse(BaseModel):
    period: PeriodResponse
    metrics: SummaryMetricsResponse
    assumptions: AssumptionsResponse
    has_data: bool


class OutcomeResponse(BaseModel):
    code: str
    label: str
    count: int
    percent: float
    is_success: bool


class DashboardOutcomesResponse(BaseModel):
    period: PeriodResponse
    outcomes: List[OutcomeResponse]
    total_with_outcome: int
    has_data: bool


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_date_range(
    start_date: Optional[datetime],
    end_date: Optional[datetime]
) -> tuple[datetime, datetime]:
    """Get date range with defaults (last 30 days)"""
    if end_date is None:
        end_date = datetime.now(timezone.utc)
    if start_date is None:
        start_date = end_date - timedelta(days=30)
    return start_date, end_date


# =============================================================================
# API ENDPOINTS
# =============================================================================

@router.get("/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    start_date: Optional[datetime] = Query(None, description="Start date (ISO 8601, UTC)"),
    end_date: Optional[datetime] = Query(None, description="End date (ISO 8601, UTC)"),
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """
    Get executive summary metrics for the Impact dashboard.

    Metrics:
    - calls_attempted: Total call_events in date range (exact)
    - calls_connected: call_events with state in {completed, answered} (exact)
    - successful_outcomes: Tasks with outcome_code in SUCCESS_OUTCOME_CODES (exact)
    - avg_days_to_resolution: Mean days from task creation to completion (exact)
    - estimated_hours_saved: calls_attempted * minutes_per_call / 60 (estimated)
    - estimated_cost_saved: hours_saved * hourly_rate (estimated)

    Auth: Requires valid JWT. org_id derived from token, never from client.
    """
    start, end = get_date_range(start_date, end_date)

    # Filter for call events in org within date range
    call_filter = and_(
        CallEvent.org_id.in_(auth.org_ids),
        CallEvent.created_at_utc >= start,
        CallEvent.created_at_utc <= end
    )

    # Calls Attempted = count(call_events) in date range
    calls_attempted_result = await session.execute(
        select(func.count(CallEvent.id)).where(call_filter)
    )
    calls_attempted = calls_attempted_result.scalar() or 0

    # Calls Connected = call_events with state in {completed, answered}
    calls_connected_result = await session.execute(
        select(func.count(CallEvent.id)).where(
            and_(
                call_filter,
                CallEvent.state.in_(['completed', 'answered'])
            )
        )
    )
    calls_connected = calls_connected_result.scalar() or 0

    # Filter for tasks in org within date range
    task_filter = and_(
        Task.org_id.in_(auth.org_ids),
        Task.created_at_utc >= start,
        Task.created_at_utc <= end
    )

    # Successful Outcomes = tasks with outcome_code in SUCCESS_OUTCOME_CODES
    # AND completed_at_utc in range
    successful_outcomes_result = await session.execute(
        select(func.count(Task.id)).where(
            and_(
                task_filter,
                Task.outcome_code.in_(SUCCESS_OUTCOME_CODES),
                Task.completed_at_utc.isnot(None),
                Task.completed_at_utc >= start,
                Task.completed_at_utc <= end
            )
        )
    )
    successful_outcomes = successful_outcomes_result.scalar() or 0

    # Avg Days to Resolution = avg(completed_at_utc - created_at_utc) in days
    # For tasks with completed_at_utc in range
    avg_resolution_result = await session.execute(
        select(
            func.avg(
                func.extract('epoch', Task.completed_at_utc - Task.created_at_utc) / 86400  # Convert to days
            )
        ).where(
            and_(
                task_filter,
                Task.completed_at_utc.isnot(None),
                Task.completed_at_utc >= start,
                Task.completed_at_utc <= end
            )
        )
    )
    avg_days_to_resolution = avg_resolution_result.scalar()
    if avg_days_to_resolution is not None:
        avg_days_to_resolution = round(avg_days_to_resolution, 1)

    # Estimated Time Saved (hours) = Calls Attempted * DEFAULT_MINUTES_SAVED_PER_CALL / 60
    estimated_hours_saved = round((calls_attempted * DEFAULT_MINUTES_SAVED_PER_CALL) / 60, 1)

    # Estimated Cost Saved ($) = Time Saved * DEFAULT_HOURLY_RATE
    estimated_cost_saved = round(estimated_hours_saved * DEFAULT_HOURLY_RATE, 2)

    # Determine if there's any data to display
    has_data = calls_attempted > 0 or successful_outcomes > 0

    return DashboardSummaryResponse(
        period=PeriodResponse(start=start, end=end),
        metrics=SummaryMetricsResponse(
            calls_attempted=calls_attempted,
            calls_connected=calls_connected,
            successful_outcomes=successful_outcomes,
            avg_days_to_resolution=avg_days_to_resolution,
            estimated_hours_saved=estimated_hours_saved,
            estimated_cost_saved=estimated_cost_saved
        ),
        assumptions=AssumptionsResponse(
            minutes_saved_per_call=DEFAULT_MINUTES_SAVED_PER_CALL,
            hourly_rate=DEFAULT_HOURLY_RATE
        ),
        has_data=has_data
    )


@router.get("/outcomes", response_model=DashboardOutcomesResponse)
async def get_dashboard_outcomes(
    start_date: Optional[datetime] = Query(None, description="Start date (ISO 8601, UTC)"),
    end_date: Optional[datetime] = Query(None, description="End date (ISO 8601, UTC)"),
    auth: AuthContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_auth_session)
):
    """
    Get outcome code distribution for visualization.

    Returns counts and percentages for each outcome code, with is_success flag
    based on SUCCESS_OUTCOME_CODES.

    Auth: Requires valid JWT. org_id derived from token, never from client.
    """
    start, end = get_date_range(start_date, end_date)

    # Filter for tasks with outcomes in date range
    task_filter = and_(
        Task.org_id.in_(auth.org_ids),
        Task.created_at_utc >= start,
        Task.created_at_utc <= end,
        Task.outcome_code.isnot(None)
    )

    # Get outcome distribution
    outcome_result = await session.execute(
        select(Task.outcome_code, func.count(Task.id))
        .where(task_filter)
        .group_by(Task.outcome_code)
        .order_by(func.count(Task.id).desc())
    )
    outcome_rows = outcome_result.all()

    # Calculate total
    total_with_outcome = sum(row[1] for row in outcome_rows)

    # Build outcome list with is_success flag
    outcomes = []
    for code, count in outcome_rows:
        outcomes.append(OutcomeResponse(
            code=code,
            label=OUTCOME_LABELS.get(code, code),
            count=count,
            percent=round((count / total_with_outcome) * 100, 1) if total_with_outcome > 0 else 0,
            is_success=code in SUCCESS_OUTCOME_CODES
        ))

    has_data = total_with_outcome > 0

    return DashboardOutcomesResponse(
        period=PeriodResponse(start=start, end=end),
        outcomes=outcomes,
        total_with_outcome=total_with_outcome,
        has_data=has_data
    )
