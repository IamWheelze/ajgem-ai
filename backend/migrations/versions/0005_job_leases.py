"""job leases for the durable worker queue

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09 04:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('lease_owner', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('claims', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('claims')
        batch_op.drop_column('lease_expires_at')
        batch_op.drop_column('lease_owner')
