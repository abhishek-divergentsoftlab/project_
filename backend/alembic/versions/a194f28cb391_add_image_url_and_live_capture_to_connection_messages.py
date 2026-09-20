"""Add image_url and is_live_capture to connection_messages

Revision ID: a194f28cb391
Revises: de7609ddea6e
Create Date: 2026-09-20 09:19:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a194f28cb391'
down_revision: Union[str, None] = 'de7609ddea6e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'connection_messages',
        sa.Column('image_url', sa.String(length=512), nullable=True)
    )
    op.add_column(
        'connection_messages',
        sa.Column('is_live_capture', sa.Boolean(), server_default=sa.text('false'), nullable=False)
    )


def downgrade() -> None:
    op.drop_column('connection_messages', 'is_live_capture')
    op.drop_column('connection_messages', 'image_url')
