"""Production schema with constraints and indexes

Revision ID: 002
Revises: 001
Create Date: 2025-01-10
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = '002'
down_revision: Union[str, None] = '001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add new columns to tasks table
    op.add_column('tasks', sa.Column('attempts', sa.Integer(), server_default='0', nullable=False))
    op.add_column('tasks', sa.Column('next_retry_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tasks', sa.Column('last_sms_sent_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('tasks', sa.Column('workflow_type', sa.String(length=50), server_default='POC_SIGNATURE', nullable=False))
    
    # Add type column to sms_events
    op.add_column('sms_events', sa.Column('type', sa.String(length=20), server_default='status_final', nullable=False))
    
    # Create indexes
    op.create_index('ix_tasks_therapist_status_created', 'tasks', ['therapist_phone', 'status', 'created_at_utc'])
    op.create_index('ix_tasks_status_retry', 'tasks', ['status', 'next_retry_at'])
    op.create_index('ix_call_events_task_created', 'call_events', ['task_id', 'created_at_utc'])
    op.create_index('ix_call_events_twilio_sid', 'call_events', ['twilio_sid'])
    
    # Create unique constraints
    op.create_unique_constraint('uq_sms_events_task_type', 'sms_events', ['task_id', 'type'])
    
    # Add check constraint for status
    op.create_check_constraint(
        'check_task_status',
        'tasks',
        "status IN ('QUEUED', 'CALLING', 'RESOLVED', 'FAILED', 'NO_ANSWER_RETRY')"
    )
    
    # Add foreign key constraints with CASCADE
    op.drop_constraint('call_events_task_id_fkey', 'call_events', type_='foreignkey')
    op.create_foreign_key(
        'call_events_task_id_fkey',
        'call_events', 'tasks',
        ['task_id'], ['id'],
        ondelete='CASCADE'
    )
    
    op.drop_constraint('sms_events_task_id_fkey', 'sms_events', type_='foreignkey')
    op.create_foreign_key(
        'sms_events_task_id_fkey',
        'sms_events', 'tasks',
        ['task_id'], ['id'],
        ondelete='CASCADE'
    )
    
    # Migrate existing data
    connection = op.get_bind()
    
    # Set default workflow_type for existing rows
    connection.execute(sa.text("UPDATE tasks SET workflow_type = 'POC_SIGNATURE' WHERE workflow_type IS NULL"))
    
    # Initialize attempts for existing rows
    connection.execute(sa.text("UPDATE tasks SET attempts = 0 WHERE attempts IS NULL"))
    
    # Generate idempotency keys for existing rows if missing
    connection.execute(sa.text("""
        UPDATE tasks 
        SET idempotency_key = encode(sha256(
            concat(patient_alias, '|', doctor_name, '|', workflow_type, '|', date(created_at_utc))::bytea
        ), 'hex')
        WHERE idempotency_key IS NULL
    """))


def downgrade() -> None:
    # Remove constraints
    op.drop_constraint('check_task_status', 'tasks', type_='check')
    op.drop_constraint('uq_sms_events_task_type', 'sms_events', type_='unique')
    
    # Remove indexes
    op.drop_index('ix_call_events_twilio_sid', table_name='call_events')
    op.drop_index('ix_call_events_task_created', table_name='call_events')
    op.drop_index('ix_tasks_status_retry', table_name='tasks')
    op.drop_index('ix_tasks_therapist_status_created', table_name='tasks')
    
    # Remove columns
    op.drop_column('sms_events', 'type')
    op.drop_column('tasks', 'workflow_type')
    op.drop_column('tasks', 'last_sms_sent_at')
    op.drop_column('tasks', 'next_retry_at')
    op.drop_column('tasks', 'attempts')
    
    # Restore original foreign keys
    op.drop_constraint('call_events_task_id_fkey', 'call_events', type_='foreignkey')
    op.create_foreign_key(
        'call_events_task_id_fkey',
        'call_events', 'tasks',
        ['task_id'], ['id']
    )
    
    op.drop_constraint('sms_events_task_id_fkey', 'sms_events', type_='foreignkey')
    op.create_foreign_key(
        'sms_events_task_id_fkey',
        'sms_events', 'tasks',
        ['task_id'], ['id']
    )