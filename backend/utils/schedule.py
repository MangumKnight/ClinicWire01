"""
Business hours and scheduling utilities for 33Health
Enforces calling only during business hours with proper retry scheduling
"""

from datetime import datetime, timedelta, time
from typing import Optional, Tuple
import pytz
import logging

logger = logging.getLogger(__name__)

# Business hours configuration
BUSINESS_START_HOUR = 9   # 9:00 AM
BUSINESS_END_HOUR = 17    # 5:00 PM
LUNCH_START_HOUR = 12     # 12:00 PM  
LUNCH_END_HOUR = 13       # 1:00 PM
BUSINESS_TIMEZONE = "US/Eastern"

def get_business_timezone():
    """Get the business timezone object"""
    return pytz.timezone(BUSINESS_TIMEZONE)

def is_business_hours(dt: Optional[datetime] = None, timezone_str: str = BUSINESS_TIMEZONE) -> bool:
    """
    Check if given datetime is within business hours
    
    Business hours: Monday-Friday 9:00-17:00 Eastern, excluding 12:00-13:00 lunch
    
    Args:
        dt: Datetime to check (defaults to now)
        timezone_str: Timezone string (defaults to US/Eastern)
        
    Returns:
        True if within business hours, False otherwise
    """
    if dt is None:
        dt = datetime.now(pytz.timezone(timezone_str))
    elif dt.tzinfo is None:
        # Assume UTC if no timezone info
        dt = pytz.utc.localize(dt).astimezone(pytz.timezone(timezone_str))
    else:
        # Convert to business timezone
        dt = dt.astimezone(pytz.timezone(timezone_str))
    
    # Check day of week (Monday=0, Sunday=6)  
    if dt.weekday() > 4:  # Saturday=5 or Sunday=6
        return False
    
    # Check hour
    hour = dt.hour
    if hour < BUSINESS_START_HOUR or hour >= BUSINESS_END_HOUR:
        return False
    
    # Check lunch break
    if LUNCH_START_HOUR <= hour < LUNCH_END_HOUR:
        return False
    
    return True

def next_business_time(dt: Optional[datetime] = None, timezone_str: str = BUSINESS_TIMEZONE) -> datetime:
    """
    Get the next available business time slot
    
    Args:
        dt: Starting datetime (defaults to now)
        timezone_str: Timezone string (defaults to US/Eastern)
        
    Returns:
        Next datetime when business is open
    """
    if dt is None:
        dt = datetime.now(pytz.timezone(timezone_str))
    elif dt.tzinfo is None:
        dt = pytz.utc.localize(dt).astimezone(pytz.timezone(timezone_str))
    else:
        dt = dt.astimezone(pytz.timezone(timezone_str))
    
    # If already in business hours, return as-is
    if is_business_hours(dt, timezone_str):
        return dt
    
    # Start checking from current time
    current = dt
    max_days_ahead = 10  # Prevent infinite loop
    
    for day_offset in range(max_days_ahead):
        # Calculate the target date
        target_date = dt.date() + timedelta(days=day_offset)
        
        # Skip weekends
        if target_date.weekday() > 4:  # Saturday=5 or Sunday=6
            continue
        
        # For today, start from current time; for future days, start at 9 AM
        if day_offset == 0:
            start_time = dt
        else:
            start_time = datetime.combine(target_date, time(BUSINESS_START_HOUR))
            start_time = pytz.timezone(timezone_str).localize(start_time)
        
        # If current day, after hours, or during lunch, adjust appropriately
        if day_offset == 0:
            if start_time.hour < BUSINESS_START_HOUR:
                start_time = start_time.replace(hour=BUSINESS_START_HOUR, minute=0, second=0, microsecond=0)
            elif LUNCH_START_HOUR <= start_time.hour < LUNCH_END_HOUR:
                start_time = start_time.replace(hour=LUNCH_END_HOUR, minute=0, second=0, microsecond=0)
        
        # Check if this time works
        if is_business_hours(start_time, timezone_str):
            return start_time
    
    # Fallback - should never reach here
    logger.error("Could not find next business time slot")
    return dt + timedelta(hours=1)

def schedule_retry(
    task_created_at: datetime, 
    attempt_number: int,
    timezone_str: str = BUSINESS_TIMEZONE
) -> datetime:
    """
    Schedule the next retry attempt respecting business hours
    
    Args:
        task_created_at: When the original task was created
        attempt_number: Which attempt this is (0=initial, 1=first retry, 2=second retry)
        timezone_str: Timezone string (defaults to US/Eastern)
        
    Returns:
        Scheduled datetime for next attempt
    """
    # Define retry delays in minutes
    RETRY_DELAYS = [
        0,    # Initial call (immediate)
        60,   # First retry: 1 hour later
        240   # Second retry: 4 hours later
    ]
    
    if attempt_number >= len(RETRY_DELAYS):
        logger.error(f"Invalid attempt number: {attempt_number}")
        return datetime.now(pytz.timezone(timezone_str)) + timedelta(hours=1)
    
    # Calculate target time based on delay
    delay_minutes = RETRY_DELAYS[attempt_number]
    target_time = task_created_at + timedelta(minutes=delay_minutes)
    
    # Find next business hours slot at or after target time
    return next_business_time(target_time, timezone_str)

def can_call_today(
    phone_number: str, 
    attempts_today: int, 
    max_attempts_per_day: int = 3
) -> bool:
    """
    Check if we can make another call to this number today
    
    Args:
        phone_number: Phone number to check
        attempts_today: Number of attempts already made today
        max_attempts_per_day: Maximum attempts allowed per day
        
    Returns:
        True if can make another call, False otherwise
    """
    return attempts_today < max_attempts_per_day

def get_business_day_key(dt: Optional[datetime] = None, timezone_str: str = BUSINESS_TIMEZONE) -> str:
    """
    Get a string key representing the current business day
    Used for tracking daily attempt limits
    
    Args:
        dt: Datetime to get key for (defaults to now)
        timezone_str: Timezone string (defaults to US/Eastern)
        
    Returns:
        String key like "2025-09-11" for the business day
    """
    if dt is None:
        dt = datetime.now(pytz.timezone(timezone_str))
    elif dt.tzinfo is None:
        dt = pytz.utc.localize(dt).astimezone(pytz.timezone(timezone_str))
    else:
        dt = dt.astimezone(pytz.timezone(timezone_str))
    
    return dt.strftime("%Y-%m-%d")

def validate_call_timing(
    dt: datetime, 
    phone_number: str, 
    daily_attempts: int = 0,
    timezone_str: str = BUSINESS_TIMEZONE
) -> Tuple[bool, str]:
    """
    Validate if a call can be made at the given time
    
    Args:
        dt: Datetime when call would be made
        phone_number: Phone number being called
        daily_attempts: Number of attempts already made today
        timezone_str: Timezone string (defaults to US/Eastern)
        
    Returns:
        Tuple of (can_call: bool, reason: str)
    """
    # Check business hours
    if not is_business_hours(dt, timezone_str):
        return False, "Outside business hours (M-F 9AM-5PM ET, excluding 12-1PM lunch)"
    
    # Check daily attempt limit
    if not can_call_today(phone_number, daily_attempts):
        return False, f"Daily attempt limit reached ({daily_attempts}/3)"
    
    return True, "OK"

# Utility functions for logging/debugging
def format_business_time(dt: datetime) -> str:
    """Format datetime in business timezone for logging"""
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    
    business_dt = dt.astimezone(pytz.timezone(BUSINESS_TIMEZONE))
    return business_dt.strftime("%Y-%m-%d %I:%M %p %Z")

def log_scheduling_decision(
    task_id: str, 
    original_time: datetime, 
    scheduled_time: datetime, 
    reason: str
):
    """Log scheduling decisions for debugging"""
    logger.info(f"[Schedule] Task {task_id[:8]}: {reason} | "
                f"Original: {format_business_time(original_time)} | "
                f"Scheduled: {format_business_time(scheduled_time)}")