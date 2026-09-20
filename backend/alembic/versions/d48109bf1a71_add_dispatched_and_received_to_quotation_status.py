"""Add dispatched and received to quotation_status enum

Revision ID: d48109bf1a71
Revises: c728e93ba452
Create Date: 2026-09-20 10:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd48109bf1a71'
down_revision: Union[str, None] = 'c728e93ba452'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE quotation_status ADD VALUE IF NOT EXISTS 'dispatched'")
        op.execute("ALTER TYPE quotation_status ADD VALUE IF NOT EXISTS 'received'")


def downgrade() -> None:
    pass
