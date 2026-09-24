"""Add business_chat to conversation_type enum and metadata to messages

Revision ID: e528abf1092a
Revises: d48109bf1a71
Create Date: 2026-09-22 21:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e528abf1092a'
down_revision: Union[str, None] = 'd48109bf1a71'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE conversation_type ADD VALUE IF NOT EXISTS 'business_chat'")
    op.add_column(
        'messages',
        sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False)
    )


def downgrade() -> None:
    op.drop_column('messages', 'metadata')
