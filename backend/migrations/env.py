"""Alembic environment for AJGEM.

Runs as the database *owner* role (AJGEM_DATABASE_ADMIN_URL, falling back to AJGEM_DATABASE_URL). The app
calls it programmatically on start-up (``ajgem.migrate``), passing an open connection in
``config.attributes["connection"]`` so in-memory SQLite and test schemas work.
"""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import context

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ajgem import models  # noqa: F401  (registers every table on Base.metadata)
from ajgem.db import Base, make_engine

config = context.config
target_metadata = Base.metadata


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite needs table rebuilds for ALTERs
    )


def run_offline() -> None:
    from ajgem.context import Settings

    s = Settings()
    context.configure(url=s.database_admin_url or s.database_url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    conn = config.attributes.get("connection")
    if conn is not None:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()
        return
    from ajgem.context import Settings

    s = Settings()
    engine = make_engine(s.database_admin_url or s.database_url)
    with engine.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
