"""Add average_rating and total_reviews to user_profiles

Revision ID: c728e93ba452
Revises: a194f28cb391
Create Date: 2026-09-20 09:36:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c728e93ba452'
down_revision: Union[str, None] = 'a194f28cb391'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'user_profiles',
        sa.Column('average_rating', sa.Numeric(precision=3, scale=2), nullable=True)
    )
    op.add_column(
        'user_profiles',
        sa.Column('total_reviews', sa.Integer(), server_default=sa.text('0'), nullable=False)
    )


def downgrade() -> None:
    op.drop_column('user_profiles', 'total_reviews')
    op.drop_column('user_profiles', 'average_rating')
