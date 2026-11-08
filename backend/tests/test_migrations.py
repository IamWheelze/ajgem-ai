"""NF14: Alembic migrations are the schema's source of truth and stay in sync with the models."""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from ajgem import migrate, rls
from ajgem.api.app import create_app
from ajgem.context import Settings
from ajgem.db import Base, make_engine

from .conftest import PG, PG_ADMIN_URL, PG_URL, _reset_pg_schema


@pytest.fixture()
def db(tmp_path):
    """(admin_engine, runtime_url, admin_url) on a fresh, empty database."""
    if PG:
        _reset_pg_schema()
        admin = create_engine(PG_ADMIN_URL)
        yield admin, PG_URL, PG_ADMIN_URL
    else:
        url = f"sqlite:///{tmp_path / 'm.db'}"
        admin = make_engine(url)
        yield admin, url, None
    admin.dispose()


def _role(runtime_url):
    return make_engine(runtime_url).url.username


def test_single_head():
    assert len(ScriptDirectory.from_config(migrate._config()).get_heads()) == 1


def test_models_and_migrations_have_no_drift(db):
    admin, runtime_url, _ = db
    migrate.upgrade(admin, _role(runtime_url))
    with admin.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"Models differ from migrations; add a migration:\n{diff}"


def test_downgrade_to_base_and_back(db):
    admin, runtime_url, _ = db
    role = _role(runtime_url)
    migrate.upgrade(admin, role)
    assert migrate.current_revision(admin) == migrate.head_revision()
    migrate.downgrade(admin, role, "base")
    assert set(inspect(admin).get_table_names()) <= {"alembic_version"}
    migrate.upgrade(admin, role)
    assert set(Base.metadata.tables) <= set(inspect(admin).get_table_names())


@pytest.mark.skipif(not PG, reason="PostgreSQL only")
def test_rls_migration_is_reversible(db):
    admin, runtime_url, _ = db
    role = _role(runtime_url)
    migrate.upgrade(admin, role)
    assert rls.verify(admin) == []
    migrate.downgrade(admin, role, "0001")
    assert rls.verify(admin) == sorted(rls.POLICIES)
    migrate.upgrade(admin, role)
    assert rls.verify(admin) == []


def _settings(tmp_path, runtime_url, admin_url, **kw):
    return Settings(database_url=runtime_url, database_admin_url=admin_url, data_dir=tmp_path / "o",
                    url_secret=b"x", **kw)


def test_app_refuses_a_pre_migration_database(db, tmp_path):
    admin, runtime_url, admin_url = db
    Base.metadata.create_all(admin)  # the old create_all-built schema, no alembic_version
    with pytest.raises(migrate.SchemaError, match="before migrations existed"):
        create_app(_settings(tmp_path, runtime_url, admin_url))


def test_auto_migrate_off_requires_a_current_schema(db, tmp_path):
    admin, runtime_url, admin_url = db
    with pytest.raises(migrate.SchemaError, match="needs"):
        create_app(_settings(tmp_path, runtime_url, admin_url, auto_migrate=False))
    migrate.upgrade(admin, _role(runtime_url))
    app = create_app(_settings(tmp_path, runtime_url, admin_url, auto_migrate=False))
    app.state.engine.dispose()
    app.state.admin_engine.dispose()


def test_stamp_adopts_an_existing_matching_schema(db, tmp_path):
    from sqlalchemy import text

    admin, runtime_url, admin_url = db
    # A database whose tables match revision 0001 but that has no migration history.
    migrate.upgrade(admin, _role(runtime_url), target="0001")
    with admin.begin() as c:
        c.execute(text("DROP TABLE alembic_version"))
    migrate.stamp(admin, "0001")
    app = create_app(_settings(tmp_path, runtime_url, admin_url))  # applies 0002 (RLS) on PostgreSQL
    assert migrate.current_revision(admin) == migrate.head_revision()
    app.state.engine.dispose()
    app.state.admin_engine.dispose()
