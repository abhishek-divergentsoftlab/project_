"""add saved rfqs table

Revision ID: 24864231ddfd
Revises: 8c4f219d34ef
Create Date: 2026-09-26 18:51:21.114078
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '24864231ddfd'
down_revision: Union[str, None] = '8c4f219d34ef'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'saved_rfqs',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('rfq_id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['rfq_id'], ['rfqs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'rfq_id', name='uq_user_saved_rfq'),
    )
    op.create_index('ix_saved_rfqs_rfq_id', 'saved_rfqs', ['rfq_id'], unique=False)
    op.create_index('ix_saved_rfqs_user_id', 'saved_rfqs', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_saved_rfqs_user_id', table_name='saved_rfqs')
    op.drop_index('ix_saved_rfqs_rfq_id', table_name='saved_rfqs')
    op.drop_table('saved_rfqs')
