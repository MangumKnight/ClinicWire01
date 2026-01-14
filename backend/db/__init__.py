"""
Database package for 33Health MCP
"""

from .database import engine, async_session_maker, get_session, close_database
from .models_multitenant import Base, Task, CallEvent, SmsEvent, Contact, Organization, User, OrgMembership, ActivityLog
from .repo_v2 import TaskRepository, CallEventRepository, SmsEventRepository, ContactRepository, ActivityLogRepository

__all__ = [
    'engine',
    'async_session_maker',
    'get_session',
    'close_database',
    'Base',
    'Task',
    'CallEvent',
    'SmsEvent',
    'Contact',
    'Organization',
    'User',
    'OrgMembership',
    'ActivityLog',
    'TaskRepository',
    'CallEventRepository',
    'SmsEventRepository',
    'ContactRepository',
    'ActivityLogRepository'
]