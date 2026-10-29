"""optional GST rate on quotations

Revision ID: b4f0gst00001
Revises: b3f0merge0001
Create Date: 2026-10-26 10:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4f0gst00001"
down_revision: Union[str, Sequence[str], None] = "b3f0merge0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("quotations", sa.Column("gst_rate", sa.Numeric(5, 2), nullable=True))


def downgrade() -> None:
    op.drop_column("quotations", "gst_rate")
