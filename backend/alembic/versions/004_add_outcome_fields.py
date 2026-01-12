"""Add outcome tracking fields

Revision ID: 004
Revises: 003
Create Date: 2024-11-04

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '004'
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade():
    """Add nullable outcome fields to tasks table"""
    # Add outcome_code column
    op.add_column('tasks', 
        sa.Column('outcome_code', sa.String(50), nullable=True)
    )
    
    # Add outcome_note column
    op.add_column('tasks',
        sa.Column('outcome_note', sa.Text(), nullable=True)
    )
    
    # Add completed_at_utc column
    op.add_column('tasks',
        sa.Column('completed_at_utc', 
                  sa.DateTime(timezone=True), 
                  nullable=True)
    )
    
    # Create index on outcome_code for reporting queries
    op.create_index('ix_tasks_outcome_code', 'tasks', ['outcome_code'])


def downgrade():
    """Remove outcome fields from tasks table"""
    # Drop index first
    op.drop_index('ix_tasks_outcome_code', 'tasks')
    
    # Drop columns
    op.drop_column('tasks', 'completed_at_utc')
    op.drop_column('tasks', 'outcome_note')
    op.drop_column('tasks', 'outcome_code')