"""add spec catalog

Revision ID: 9a41c7e2b5d0
Revises: 33f0b331948b
Create Date: 2026-10-13 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '9a41c7e2b5d0'
down_revision: Union[str, None] = '33f0b331948b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('spec_catalog',
    sa.Column('family_key', sa.String(length=120), nullable=False),
    sa.Column('display_name', sa.String(length=160), nullable=False),
    sa.Column('category', sa.String(length=120), nullable=True),
    sa.Column('hsn_heading', sa.String(length=4), nullable=True),
    sa.Column('attributes', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.Column('source', sa.String(length=20), server_default='llm', nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_spec_catalog_family_key', 'spec_catalog', ['family_key'], unique=True)
    op.create_index('ix_spec_catalog_hsn_heading', 'spec_catalog', ['hsn_heading'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_spec_catalog_hsn_heading', table_name='spec_catalog')
    op.drop_index('ix_spec_catalog_family_key', table_name='spec_catalog')
    op.drop_table('spec_catalog')
