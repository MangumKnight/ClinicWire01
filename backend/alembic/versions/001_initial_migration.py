"""Initial migration with tasks, call_events, and sms_events tables

Revision ID: 001
Revises: 
Create Date: 2025-01-10
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic
revision: str = '001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create tasks table
    op.create_table('tasks',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('source', sa.String(length=10), nullable=False),
        sa.Column('workflow_type', sa.String(length=50), nullable=False),
        sa.Column('patient_alias', sa.String(length=255), nullable=False),
        sa.Column('doctor_name', sa.String(length=255), nullable=False),
        sa.Column('therapist_phone', sa.String(length=20), nullable=True),
        sa.Column('idempotency_key', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False, server_default='QUEUED'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('sheet_row_id', sa.Integer(), nullable=True),
        sa.Column('sheet_ingested_at_utc', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at_utc', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tasks_idempotency_key'), 'tasks', ['idempotency_key'], unique=True)

    # Create call_events table
    op.create_table('call_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('twilio_sid', sa.String(length=50), nullable=True),
        sa.Column('state', sa.String(length=20), nullable=False),
        sa.Column('duration_sec', sa.Integer(), nullable=True),
        sa.Column('raw_status_json', sa.JSON(), nullable=True),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_call_events_task_id'), 'call_events', ['task_id'], unique=False)

    # Create sms_events table
    op.create_table('sms_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('to_number', sa.String(length=20), nullable=False),
        sa.Column('body_hash', sa.String(length=64), nullable=False),
        sa.Column('provider_sid', sa.String(length=50), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sms_events_task_id'), 'sms_events', ['task_id'], unique=False)


def downgrade() -> None:
    # Drop tables in reverse order due to foreign key constraints
    op.drop_index(op.f('ix_sms_events_task_id'), table_name='sms_events')
    op.drop_table('sms_events')
    
    op.drop_index(op.f('ix_call_events_task_id'), table_name='call_events')
    op.drop_table('call_events')
    
    op.drop_index(op.f('ix_tasks_idempotency_key'), table_name='tasks')
    op.drop_table('tasks')