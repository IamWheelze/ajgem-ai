"""row-level security policies (NF01)

Enables and forces RLS on every tenant table, installs the policies, the
ajgem_visible_projects() helper and the runtime role's grants (see ajgem/rls.py).
PostgreSQL only; a no-op on SQLite.

The runtime role is taken from the migration context (config attribute
"runtime_role", set by ajgem.migrate) or the username in AJGEM_DATABASE_URL.

Revision ID: 0002
Revises: 0001
"""
from typing import Sequence, Union

from alembic import context, op
from sqlalchemy import text

from ajgem import rls

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _runtime_role() -> str:
    role = context.config.attributes.get("runtime_role")
    if not role:
        from sqlalchemy.engine import make_url

        from ajgem.context import Settings

        role = make_url(Settings().database_url).username
    if not role:
        raise RuntimeError("Cannot determine the runtime role for RLS grants; set AJGEM_DATABASE_URL")
    return role


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for sql in rls.statements(_runtime_role()):
        bind.execute(text(sql))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in rls.POLICIES:
        bind.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {table}"))
        bind.execute(text(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY"))
        bind.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    bind.execute(text("DROP FUNCTION IF EXISTS ajgem_visible_projects()"))
