"""Schema migrations (NF14): Alembic, run as the database owner role.

``upgrade`` is called automatically on start-up when ``AJGEM_AUTO_MIGRATE`` is on (the default). With it
off, start-up only checks that the database is already at the latest revision and refuses to serve
otherwise; run ``python -m ajgem migrate`` as a deployment step instead.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


class SchemaError(RuntimeError):
    pass


def _config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_INI.parent / "migrations"))
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _check_not_legacy(conn) -> None:
    tables = set(inspect(conn).get_table_names())
    if "organisations" in tables and "alembic_version" not in tables:
        raise SchemaError(
            "This database was created before migrations existed (no alembic_version table). For a development "
            "database, delete it (backend/var) and restart. To adopt an existing database whose schema matches "
            "revision 0001, run `python -m ajgem migrate --stamp 0001` once, then restart.")


def upgrade(admin_engine: Engine, runtime_role: str | None, target: str = "head") -> None:
    with admin_engine.begin() as conn:
        _check_not_legacy(conn)
        cfg = _config()
        cfg.attributes["connection"] = conn
        cfg.attributes["runtime_role"] = runtime_role
        command.upgrade(cfg, target)


def downgrade(admin_engine: Engine, runtime_role: str | None, target: str) -> None:
    with admin_engine.begin() as conn:
        cfg = _config()
        cfg.attributes["connection"] = conn
        cfg.attributes["runtime_role"] = runtime_role
        command.downgrade(cfg, target)


def stamp(admin_engine: Engine, revision: str) -> None:
    with admin_engine.begin() as conn:
        cfg = _config()
        cfg.attributes["connection"] = conn
        command.stamp(cfg, revision)


def ensure_at_head(engine: Engine) -> None:
    cur, head = current_revision(engine), head_revision()
    if cur != head:
        raise SchemaError(f"Database schema is at {cur or 'nothing'}, the application needs {head}. "
                          "Run `python -m ajgem migrate` (or set AJGEM_AUTO_MIGRATE=1).")
