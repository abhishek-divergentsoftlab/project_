"""merge the security and order-integrity heads

Revision ID: b3f0merge0001
Revises: b1f0sec00001, b2f0pay00001
Create Date: 2026-10-25 14:00:00.000000

Both branches are independent and already applied; this revision only joins them.
"""

from typing import Sequence, Union

revision: str = "b3f0merge0001"
down_revision: Union[str, Sequence[str], None] = ("b1f0sec00001", "b2f0pay00001")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
