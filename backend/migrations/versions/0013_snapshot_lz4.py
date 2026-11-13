"""answer snapshot columns use lz4 compression on PostgreSQL

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-13 09:00:00

Large answer snapshots (up to 100,000 rows) are written once per question. PostgreSQL's default pglz
compression made that write the slowest step of the worst-case threshold question; lz4 is several
times faster for a similar size (NF09). Servers built without lz4 keep pglz.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0013'
down_revision: Union[str, None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (("answer_snapshots", "inputs"), ("answer_snapshots", "result"))


def _lz4_available(bind) -> bool:
    try:
        return bind.execute(sa.text(
            "SELECT 'lz4' = ANY(string_to_array(current_setting('default_toast_compression', true), ',')) "
            "OR EXISTS (SELECT 1 FROM pg_settings WHERE name = 'default_toast_compression' "
            "AND 'lz4' = ANY(enumvals))")).scalar()
    except Exception:  # noqa: BLE001
        return False


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql" or not _lz4_available(bind):
        return
    for table, column in COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET COMPRESSION lz4")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table, column in COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET COMPRESSION pglz")
