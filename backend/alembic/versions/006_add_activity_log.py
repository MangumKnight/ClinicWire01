"""Add activity_log table for unified event tracking

Revision ID: 006
Revises: 005
Create Date: 2026-01-14

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '006'
down_revision = '005'
branch_labels = None
depends_on = None


def upgrade():
    """Create activity_log table with RLS"""

    # Create activity_log table
    op.create_table(
        'activity_log',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('org_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('orgs.id', ondelete='CASCADE'), nullable=False),
        sa.Column('event_type', sa.String(50), nullable=False),
        sa.Column('task_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('tasks.id', ondelete='SET NULL'), nullable=True),
        sa.Column('actor_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('summary', sa.Text(), nullable=False),
        sa.Column('details', postgresql.JSON(), nullable=True),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # Create indexes
    op.create_index('ix_activity_log_org_id', 'activity_log', ['org_id'])
    op.create_index('ix_activity_log_event_type', 'activity_log', ['event_type'])
    op.create_index('ix_activity_log_task_id', 'activity_log', ['task_id'])
    op.create_index('ix_activity_log_org_created', 'activity_log', ['org_id', 'created_at_utc'])
    op.create_index('ix_activity_log_org_type_created', 'activity_log', ['org_id', 'event_type', 'created_at_utc'])
    op.create_index('ix_activity_log_task_created', 'activity_log', ['task_id', 'created_at_utc'])

    # Enable RLS
    op.execute("ALTER TABLE activity_log ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY activity_log_tenant_isolation ON activity_log
        FOR ALL
        USING (
            CASE
                WHEN current_setting('app.current_org_id', true) = '' THEN false
                WHEN current_setting('app.current_org_id', true) IS NULL THEN false
                ELSE org_id = current_setting('app.current_org_id', true)::uuid
            END
        )
        WITH CHECK (
            CASE
                WHEN current_setting('app.current_org_id', true) = '' THEN false
                WHEN current_setting('app.current_org_id', true) IS NULL THEN false
                ELSE org_id = current_setting('app.current_org_id', true)::uuid
            END
        )
    """)


def downgrade():
    """Drop activity_log table"""
    op.execute("DROP POLICY IF EXISTS activity_log_tenant_isolation ON activity_log")
    op.execute("ALTER TABLE activity_log DISABLE ROW LEVEL SECURITY")
    op.drop_index('ix_activity_log_task_created', 'activity_log')
    op.drop_index('ix_activity_log_org_type_created', 'activity_log')
    op.drop_index('ix_activity_log_org_created', 'activity_log')
    op.drop_index('ix_activity_log_task_id', 'activity_log')
    op.drop_index('ix_activity_log_event_type', 'activity_log')
    op.drop_index('ix_activity_log_org_id', 'activity_log')
    op.drop_table('activity_log')
