"""Add Row-Level Security policies for tenant isolation

Revision ID: 005
Revises: 004
Create Date: 2026-01-14

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '005'
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade():
    """Enable RLS and create policies for tenant tables"""

    # Enable RLS on tasks table
    op.execute("ALTER TABLE tasks ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tasks_tenant_isolation ON tasks
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

    # Enable RLS on contacts table
    op.execute("ALTER TABLE contacts ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY contacts_tenant_isolation ON contacts
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

    # Enable RLS on sms_events table
    op.execute("ALTER TABLE sms_events ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY sms_events_tenant_isolation ON sms_events
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

    # Note: call_events doesn't have org_id directly - it inherits from task
    # RLS is enforced via task join in application code


def downgrade():
    """Remove RLS policies and disable RLS"""

    # Drop policies and disable RLS on sms_events
    op.execute("DROP POLICY IF EXISTS sms_events_tenant_isolation ON sms_events")
    op.execute("ALTER TABLE sms_events DISABLE ROW LEVEL SECURITY")

    # Drop policies and disable RLS on contacts
    op.execute("DROP POLICY IF EXISTS contacts_tenant_isolation ON contacts")
    op.execute("ALTER TABLE contacts DISABLE ROW LEVEL SECURITY")

    # Drop policies and disable RLS on tasks
    op.execute("DROP POLICY IF EXISTS tasks_tenant_isolation ON tasks")
    op.execute("ALTER TABLE tasks DISABLE ROW LEVEL SECURITY")
