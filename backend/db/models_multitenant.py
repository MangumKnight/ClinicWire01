"""
Database models for multi-tenant ClinicWire
Using SQLAlchemy 2.0 with PostgreSQL and Row-Level Security
"""

from datetime import datetime
from typing import Optional, List
import uuid
from sqlalchemy import String, Text, DateTime, Integer, JSON, ForeignKey, UniqueConstraint, Index, CheckConstraint, Boolean, LargeBinary
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

class Base(DeclarativeBase):
    pass

# ===================
# AUTHENTICATION TABLES
# ===================

class Organization(Base):
    """Organizations are the top-level tenant"""
    __tablename__ = "orgs"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Basic info
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )
    
    # Soft delete
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True
    )
    
    # Features/limits
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, server_default='{}')
    
    # Relationships
    users: Mapped[List["OrgMembership"]] = relationship(back_populates="org", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index('ix_orgs_deleted', 'deleted_at'),
        CheckConstraint("slug ~ '^[a-z0-9-]+$'", name='check_org_slug_format'),
    )

class User(Base):
    """Users can belong to multiple organizations"""
    __tablename__ = "users"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Authentication
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Profile
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # E.164 format
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )
    
    # Auth tracking
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    orgs: Mapped[List["OrgMembership"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[List["UserSession"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    
    __table_args__ = (
        CheckConstraint("email ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Z|a-z]{2,}$'", name='check_user_email_format'),
    )

class OrgMembership(Base):
    """Junction table for users <-> organizations with role"""
    __tablename__ = "org_memberships"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Foreign keys
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )
    
    # Role
    role: Mapped[str] = mapped_column(
        String(20), 
        nullable=False,
        server_default='member'
    )
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    
    # Relationships
    org: Mapped["Organization"] = relationship(back_populates="users")
    user: Mapped["User"] = relationship(back_populates="orgs")
    
    __table_args__ = (
        UniqueConstraint('org_id', 'user_id', name='uq_org_memberships_org_user'),
        Index('ix_org_memberships_user_id', 'user_id'),
        CheckConstraint(
            "role IN ('owner', 'admin', 'member', 'guest')",
            name='check_membership_role'
        ),
    )

class UserSession(Base):
    """JWT sessions for users"""
    __tablename__ = "user_sessions"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Foreign keys
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False
    )
    
    # Session data
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)  # SHA256 of JWT
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    
    # Metadata
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)  # IPv6 max length
    user_agent: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    
    # Relationships
    user: Mapped["User"] = relationship(back_populates="sessions")
    
    __table_args__ = (
        Index('ix_user_sessions_user_expires', 'user_id', 'expires_at'),
    )

class AuthCode(Base):
    """Magic link codes for authentication"""
    __tablename__ = "auth_codes"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Code details
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)  # SHA256 of code
    
    # Expiration
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Metadata
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    
    __table_args__ = (
        Index('ix_auth_codes_email_expires', 'email', 'expires_at'),
    )

# ===================
# TENANT TABLES (with org_id)
# ===================

class Task(Base):
    """Core task table - now multi-tenant"""
    __tablename__ = "tasks"
    
    # Primary key - UUIDv4
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Tenant column - REQUIRED for RLS
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    
    # Timestamps
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )
    
    # Core fields
    workflow_type: Mapped[str] = mapped_column(String(50), nullable=False, server_default='POC_SIGNATURE')
    patient_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    doctor_name: Mapped[str] = mapped_column(String(255), nullable=False)
    doctor_phone: Mapped[str] = mapped_column(String(20), nullable=False)  # E.164 format - who gets called
    therapist_phone: Mapped[str] = mapped_column(String(20), nullable=False)  # E.164 format - who gets SMS updates
    
    # Status and retry tracking
    status: Mapped[str] = mapped_column(
        String(20), 
        nullable=False,
        server_default='QUEUED'
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default='0')
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Idempotency
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    
    # Additional data
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # SMS tracking
    last_sms_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # User tracking
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    # Outcome v2 fields (added by migration 004_add_outcome_fields.py)
    outcome_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    outcome_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    call_events: Mapped[List["CallEvent"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    sms_events: Mapped[List["SmsEvent"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        # Org-scoped uniqueness
        UniqueConstraint('org_id', 'idempotency_key', name='uq_tasks_org_idempotency'),
        Index('ix_tasks_org_therapist_status_created', 'org_id', 'therapist_phone', 'status', 'created_at_utc'),
        Index('ix_tasks_org_status_retry', 'org_id', 'status', 'next_retry_at'),
        CheckConstraint(
            "status IN ('QUEUED', 'CALLING', 'RESOLVED', 'FAILED', 'NO_ANSWER_RETRY')",
            name='check_task_status'
        ),
    )

class CallEvent(Base):
    """Call events - now multi-tenant"""
    __tablename__ = "call_events"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Tenant column
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False
    )
    
    # Call tracking
    twilio_sid: Mapped[Optional[str]] = mapped_column(String(50), nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    duration_sec: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    raw_status_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    
    # Relationship
    task: Mapped["Task"] = relationship(back_populates="call_events")
    
    # Indexes
    __table_args__ = (
        Index('ix_call_events_org_task_created', 'org_id', 'task_id', 'created_at_utc'),
    )

class SmsEvent(Base):
    """SMS events - now multi-tenant"""
    __tablename__ = "sms_events"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Tenant column
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("tasks.id", ondelete="CASCADE"),
        nullable=False
    )
    
    # SMS tracking
    to_number: Mapped[str] = mapped_column(String(20), nullable=False)  # E.164 format
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA256 of message
    provider_sid: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False, server_default='status_final')
    
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    
    # Relationship
    task: Mapped["Task"] = relationship(back_populates="sms_events")
    
    # Constraints - prevent duplicate SMS per task
    __table_args__ = (
        UniqueConstraint('task_id', 'type', name='uq_sms_events_task_type'),
    )

class Contact(Base):
    """Saved contacts - now multi-tenant"""
    __tablename__ = "contacts"
    
    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
    )
    
    # Tenant column
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    
    # Contact info
    patient_alias: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    doctor_name: Mapped[str] = mapped_column(Text, nullable=False)
    office_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_e164: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fax_e164: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # Timestamps
    last_verified_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        server_default=func.now()
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), 
        server_default=func.now(),
        nullable=False
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )
    
    # User tracking
    created_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), 
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )
    
    # Indexes
    __table_args__ = (
        Index('idx_contacts_org_doctor_office', 'org_id', func.lower(doctor_name), func.lower(office_name)),
    )


class ActivityLog(Base):
    """
    Unified activity log for all events in the system.
    Provides a single timeline of activity per organization.
    """
    __tablename__ = "activity_log"

    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )

    # Tenant column - REQUIRED for RLS
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )

    # Event classification
    event_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True
    )
    # Event types:
    # TASK_CREATED, TASK_STATUS_CHANGED, TASK_DELETED
    # CALL_INITIATED, CALL_COMPLETED, CALL_NO_ANSWER, CALL_FAILED
    # SMS_SENT, SMS_DELIVERED, SMS_FAILED
    # USER_LOGIN, USER_LOGOUT

    # Related entities (optional)
    task_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tasks.id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    # Actor (who triggered this event - null for system events)
    actor_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True
    )

    # Human-readable summary
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # Additional context (JSON)
    details: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)

    # Timestamp
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )

    # Indexes for common queries
    __table_args__ = (
        Index('ix_activity_log_org_created', 'org_id', 'created_at_utc'),
        Index('ix_activity_log_org_type_created', 'org_id', 'event_type', 'created_at_utc'),
        Index('ix_activity_log_task_created', 'task_id', 'created_at_utc'),
    )