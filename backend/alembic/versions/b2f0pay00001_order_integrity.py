"""order integrity: one escrow vault per order, deposit reports, quote snapshots

Revision ID: b2f0pay00001
Revises: 9a41c7e2b5d0
Create Date: 2026-10-25 12:00:00.000000

* ``escrow_accounts``: the vault is keyed on the order (``quotation_id``, now
  UNIQUE); the UNIQUE index on ``connection_id`` becomes a plain index so a
  deal room can hold one vault per successive order. New nullable columns
  record the buyer's reported deposit (a report, not proof: an operator
  confirms it) and an operator's rejection reason. ``status`` is a plain
  string column, so the new ``deposit_reported`` value needs no enum change.
* ``quotations.rfq_snapshot``: the listing's title / specs / destination as
  they were when the quote was accepted, so the PO and invoice stay frozen.
* ``deal_disputes``: proposer and proposed split of a two-step mutual
  settlement.

Backward compatible: every new column is nullable and nothing is rewritten.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2f0pay00001"
down_revision: Union[str, None] = "9a41c7e2b5d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_escrow_accounts_connection_id", table_name="escrow_accounts")
    op.create_index("ix_escrow_accounts_connection_id", "escrow_accounts", ["connection_id"])
    op.drop_index("ix_escrow_accounts_quotation_id", table_name="escrow_accounts")
    op.create_index("ix_escrow_accounts_quotation_id", "escrow_accounts", ["quotation_id"], unique=True)

    op.add_column("escrow_accounts", sa.Column("deposit_method", sa.String(length=32), nullable=True))
    op.add_column("escrow_accounts", sa.Column("deposit_reference", sa.String(length=120), nullable=True))
    op.add_column("escrow_accounts", sa.Column("deposit_reported_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("escrow_accounts", sa.Column("deposit_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("escrow_accounts", sa.Column("deposit_rejection_reason", sa.Text(), nullable=True))

    op.add_column("quotations", sa.Column("rfq_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True))

    op.add_column(
        "deal_disputes",
        sa.Column("settlement_proposed_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("deal_disputes", sa.Column("settlement_release_percent", sa.Numeric(5, 2), nullable=True))
    op.add_column("deal_disputes", sa.Column("settlement_proposed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "deal_disputes_settlement_proposed_by_id_fkey",
        "deal_disputes", "users", ["settlement_proposed_by_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("deal_disputes_settlement_proposed_by_id_fkey", "deal_disputes", type_="foreignkey")
    op.drop_column("deal_disputes", "settlement_proposed_at")
    op.drop_column("deal_disputes", "settlement_release_percent")
    op.drop_column("deal_disputes", "settlement_proposed_by_id")
    op.drop_column("quotations", "rfq_snapshot")
    for col in (
        "deposit_rejection_reason", "deposit_confirmed_at", "deposit_reported_at",
        "deposit_reference", "deposit_method",
    ):
        op.drop_column("escrow_accounts", col)
    op.drop_index("ix_escrow_accounts_quotation_id", table_name="escrow_accounts")
    op.create_index("ix_escrow_accounts_quotation_id", "escrow_accounts", ["quotation_id"])
    # Restoring the per-connection UNIQUE index fails if a deal room now holds
    # several vaults; resolve those by hand before downgrading.
    op.drop_index("ix_escrow_accounts_connection_id", table_name="escrow_accounts")
    op.create_index("ix_escrow_accounts_connection_id", "escrow_accounts", ["connection_id"], unique=True)
