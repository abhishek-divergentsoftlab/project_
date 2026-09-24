"""add shipments table for multi-modal logistics and tracking

Revision ID: 8c4f219d34ef
Revises: 7b3e129f12ab
Create Date: 2026-09-23 16:50:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = '8c4f219d34ef'
down_revision: Union[str, None] = '7b3e129f12ab'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'shipments',
        sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
        sa.Column('connection_id', sa.UUID(), nullable=False),
        sa.Column('quotation_id', sa.UUID(), nullable=True),
        sa.Column('sender_id', sa.UUID(), nullable=False),
        sa.Column('receiver_id', sa.UUID(), nullable=False),
        sa.Column('tracking_number', sa.String(length=64), nullable=False),
        sa.Column('carrier_name', sa.String(length=120), nullable=False),
        sa.Column('carrier_service', sa.String(length=120), nullable=True),
        sa.Column('shipping_mode', sa.String(length=32), server_default='road', nullable=False),
        sa.Column('status', sa.String(length=32), server_default='booked', nullable=False),
        sa.Column('origin_city', sa.String(length=120), nullable=False),
        sa.Column('origin_state', sa.String(length=120), nullable=True),
        sa.Column('origin_country', sa.String(length=120), nullable=False),
        sa.Column('origin_address', sa.Text(), nullable=True),
        sa.Column('destination_city', sa.String(length=120), nullable=False),
        sa.Column('destination_state', sa.String(length=120), nullable=True),
        sa.Column('destination_country', sa.String(length=120), nullable=False),
        sa.Column('destination_address', sa.Text(), nullable=True),
        sa.Column('weight_kg', sa.Numeric(precision=12, scale=3), server_default='0.000', nullable=False),
        sa.Column('volume_cbm', sa.Numeric(precision=10, scale=3), nullable=True),
        sa.Column('package_count', sa.Integer(), server_default='1', nullable=False),
        sa.Column('package_type', sa.String(length=60), server_default='Boxes', nullable=False),
        sa.Column('bill_of_lading_number', sa.String(length=100), nullable=True),
        sa.Column('estimated_delivery_date', sa.DateTime(timezone=True), nullable=True),
        sa.Column('dispatched_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('tracking_events', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['quotation_id'], ['quotations.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['sender_id'], ['users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['receiver_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_shipments_connection_id', 'shipments', ['connection_id'], unique=False)
    op.create_index('ix_shipments_tracking_number', 'shipments', ['tracking_number'], unique=True)
    op.create_index('ix_shipments_sender_id', 'shipments', ['sender_id'], unique=False)
    op.create_index('ix_shipments_receiver_id', 'shipments', ['receiver_id'], unique=False)
    op.create_index('ix_shipments_status', 'shipments', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_shipments_status', table_name='shipments')
    op.drop_index('ix_shipments_receiver_id', table_name='shipments')
    op.drop_index('ix_shipments_sender_id', table_name='shipments')
    op.drop_index('ix_shipments_tracking_number', table_name='shipments')
    op.drop_index('ix_shipments_connection_id', table_name='shipments')
    op.drop_table('shipments')
