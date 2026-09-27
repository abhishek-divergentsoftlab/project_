"""add_matching_preferences_to_user_profiles

Revision ID: c787ebf5ebc4
Revises: 24864231ddfd
Create Date: 2026-09-27 16:45:01.025222
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c787ebf5ebc4'
down_revision: Union[str, None] = '24864231ddfd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    op.add_column(
        "user_profiles",
        sa.Column(
            "matching_preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("user_profiles", "matching_preferences")
