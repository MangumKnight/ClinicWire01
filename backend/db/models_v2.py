"""
Database models for 33Health MCP - Production Schema
Using SQLAlchemy 2.0 with asyncpg
"""

from datetime import datetime
from typing import Optional
import uuid
from sqlalchemy import String, Text, DateTime, Integer, JSON, ForeignKey, UniqueConstraint, Index, CheckConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

class Base(DeclarativeBase):
    pass

class Task(Base):
    __tablename__ = "tasks"
    
    # Primary key - UUIDv4
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
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
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    
    # Additional data
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    
    # SMS tracking
    last_sms_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Outcome tracking (v2 - feature flagged)
    outcome_code: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    outcome_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    
    # Relationships
    call_events: Mapped[list["CallEvent"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    sms_events: Mapped[list["SmsEvent"]] = relationship(back_populates="task", cascade="all, delete-orphan")
    
    # Indexes
    __table_args__ = (
        Index('ix_tasks_therapist_status_created', 'therapist_phone', 'status', 'created_at_utc'),
        Index('ix_tasks_status_retry', 'status', 'next_retry_at'),
        CheckConstraint(
            "status IN ('QUEUED', 'CALLING', 'RESOLVED', 'FAILED', 'NO_ANSWER_RETRY')",
            name='check_task_status'
        ),
    )

class CallEvent(Base):
    __tablename__ = "call_events"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
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
        Index('ix_call_events_task_created', 'task_id', 'created_at_utc'),
    )

class SmsEvent(Base):
    __tablename__ = "sms_events"
    
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
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
    __tablename__ = "contacts"
    
    # Primary key
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), 
        primary_key=True, 
        default=uuid.uuid4
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
    
    # Indexes
    __table_args__ = (
        Index('idx_contacts_doctor_office', func.lower(doctor_name), func.lower(office_name)),
    )