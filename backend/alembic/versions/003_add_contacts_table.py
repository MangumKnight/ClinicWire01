"""add contacts table

Revision ID: 003
Revises: 002
Create Date: 2025-09-24 11:55:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '003'
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade():
    # Create contacts table
    op.create_table('contacts',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('doctor_name', sa.Text(), nullable=False),
        sa.Column('office_name', sa.Text(), nullable=True),
        sa.Column('phone_e164', sa.Text(), nullable=True),
        sa.Column('fax_e164', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('last_verified_at', sa.DateTime(timezone=True), nullable=True, server_default=sa.text('now()')),
        sa.Column('created_at_utc', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at_utc', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()'))
    )
    
    # Create index for doctor/office search
    op.create_index(
        'idx_contacts_doctor_office',
        'contacts',
        [sa.text('lower(doctor_name)'), sa.text('lower(office_name)')],
        postgresql_using='btree'
    )


def downgrade():
    op.drop_index('idx_contacts_doctor_office', table_name='contacts')
    op.drop_table('contacts')