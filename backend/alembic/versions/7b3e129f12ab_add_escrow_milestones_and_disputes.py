"""add escrow milestones and deal disputes

Revision ID: 7b3e129f12ab
Revises: 29496aa34764
Create Date: 2026-09-23 16:25:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '7b3e129f12ab'
down_revision: Union[str, None] = '29496aa34764'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. escrow_accounts
    op.create_table(
        'escrow_accounts',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('connection_id', sa.UUID(), nullable=False),
        sa.Column('quotation_id', sa.UUID(), nullable=False),
        sa.Column('buyer_id', sa.UUID(), nullable=False),
        sa.Column('seller_id', sa.UUID(), nullable=False),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='USD'),
        sa.Column('total_amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('funded_amount', sa.Numeric(precision=18, scale=4), server_default='0.00', nullable=False),
        sa.Column('released_amount', sa.Numeric(precision=18, scale=4), server_default='0.00', nullable=False),
        sa.Column('refunded_amount', sa.Numeric(precision=18, scale=4), server_default='0.00', nullable=False),
        sa.Column('status', sa.String(length=32), server_default='pending_deposit', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['quotation_id'], ['quotations.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['buyer_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['seller_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_escrow_accounts_connection_id', 'escrow_accounts', ['connection_id'], unique=False)
    op.create_index('ix_escrow_accounts_quotation_id', 'escrow_accounts', ['quotation_id'], unique=False)
    op.create_index('ix_escrow_accounts_buyer_id', 'escrow_accounts', ['buyer_id'], unique=False)
    op.create_index('ix_escrow_accounts_seller_id', 'escrow_accounts', ['seller_id'], unique=False)
    op.create_index('ix_escrow_accounts_status', 'escrow_accounts', ['status'], unique=False)

    # 2. escrow_milestones
    op.create_table(
        'escrow_milestones',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('escrow_account_id', sa.UUID(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('percentage', sa.Numeric(precision=5, scale=2), nullable=False),
        sa.Column('amount', sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column('order_index', sa.Integer(), server_default='0', nullable=False),
        sa.Column('status', sa.String(length=32), server_default='pending', nullable=False),
        sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('release_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['escrow_account_id'], ['escrow_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_escrow_milestones_escrow_id', 'escrow_milestones', ['escrow_account_id'], unique=False)
    op.create_index('ix_escrow_milestones_status', 'escrow_milestones', ['status'], unique=False)

    # 3. deal_disputes
    op.create_table(
        'deal_disputes',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('connection_id', sa.UUID(), nullable=False),
        sa.Column('escrow_account_id', sa.UUID(), nullable=True),
        sa.Column('raised_by_id', sa.UUID(), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('category', sa.String(length=64), server_default='quality', nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('severity', sa.String(length=32), server_default='medium', nullable=False),
        sa.Column('status', sa.String(length=32), server_default='open', nullable=False),
        sa.Column('suggested_resolution', sa.Text(), nullable=True),
        sa.Column('resolution_notes', sa.Text(), nullable=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['escrow_account_id'], ['escrow_accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['raised_by_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_deal_disputes_connection_id', 'deal_disputes', ['connection_id'], unique=False)
    op.create_index('ix_deal_disputes_escrow_account_id', 'deal_disputes', ['escrow_account_id'], unique=False)
    op.create_index('ix_deal_disputes_status', 'deal_disputes', ['status'], unique=False)


def downgrade() -> None:
    op.drop_table('deal_disputes')
    op.drop_table('escrow_milestones')
    op.drop_table('escrow_accounts')
