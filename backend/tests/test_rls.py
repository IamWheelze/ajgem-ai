"""NF01: PostgreSQL row-level security, tested at the database layer with the application checks bypassed.

Runs only with AJGEM_TEST_PG=1 (CI runs it). Each test seeds data through the API, then queries as the
runtime role with raw SQL to show the database itself refuses cross-tenant access.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from ajgem import rls
from ajgem.db import Base

from .conftest import CSV_MAPPING, PG, make_assay_csv

pytestmark = pytest.mark.skipif(not PG, reason="PostgreSQL RLS tests: set AJGEM_TEST_PG=1")


@pytest.fixture()
def seeded(team, app):
    an, r1, pid = team["analyst"], team["rev1"], team["pid"]
    v = an.upload(pid, make_assay_csv(3), "a.csv").json()
    pv = an.post(f"/versions/{v['version_id']}/import-previews", json={"mapping": CSV_MAPPING}).json()
    an.post(f"/imports/{pv['id']}/commit", json={"preview_hash": pv["preview_hash"]})
    return {**team, "app": app}


def _count(conn, table, user=None, org=None):
    conn.execute(text("SELECT set_config('app.user_id', :u, true), set_config('app.org_id', :o, true)"),
                 {"u": user or "", "o": org or ""})
    return conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()


def _runtime(app):
    return app.state.engine.connect()


def _org_of(app, user_id):
    with app.state.admin_engine.connect() as c:
        return c.execute(text("SELECT organisation_id FROM users WHERE id = :u"), {"u": user_id}).scalar()


def test_every_table_has_forced_rls_and_a_policy(app):
    assert set(Base.metadata.tables) == set(rls.POLICIES)  # a new table without a policy fails here
    assert rls.verify(app.state.admin_engine) == []


def test_runtime_role_cannot_bypass_and_owner_role_is_refused_for_serving(app):
    with app.state.engine.connect() as c:
        row = c.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")).one()
    assert (row.rolsuper, row.rolbypassrls) == (False, False)
    with pytest.raises(rls.RlsBypassRole):
        rls.ensure_runtime_role_is_constrained(app.state.admin_engine)


def test_no_context_sees_nothing(seeded):
    app = seeded["app"]
    with app.state.admin_engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM records")).scalar() > 0  # data exists (owner bypass)
    with _runtime(app) as c:
        for table in ("organisations", "users", "projects", "records", "record_revisions", "source_anchors",
                      "document_versions", "audit_events", "job_tasks"):
            assert _count(c, table) == 0, table


def test_other_organisation_sees_nothing_even_with_a_forged_project_id(seeded):
    app, ids = seeded["app"], seeded["ids"]
    outsider_org = _org_of(app, ids["outsider"])
    with _runtime(app) as c:
        for table in ("projects", "records", "record_revisions", "documents", "memberships", "audit_events", "users"):
            n = _count(c, table, ids["outsider"], outsider_org)
            assert n == (1 if table == "users" else 0), table  # only the outsider's own user row
        # Claiming the victim's organisation id without being its member still shows no project content.
        victim_org = _org_of(app, ids["analyst"])
        assert _count(c, "records", ids["outsider"], victim_org) == 0


def test_project_content_needs_live_membership(seeded):
    app, ids, pid = seeded["app"], seeded["ids"], seeded["pid"]
    org = _org_of(app, ids["analyst"])
    with _runtime(app) as c:
        member_records = _count(c, "records", ids["analyst"], org)
        assert member_records > 0
    # Same-organisation user without membership: no project rows, no content.
    with app.state.admin_engine.begin() as c:
        c.execute(text("INSERT INTO users (id, organisation_id, email, name, active, created_at) "
                       "VALUES ('nonmember-1', :o, 'nm@kano.test', 'nm', true, now())"), {"o": org})
    with _runtime(app) as c:
        assert _count(c, "projects", "nonmember-1", org) == 0
        assert _count(c, "records", "nonmember-1", org) == 0
    # Revocation takes effect in the database immediately.
    assert seeded["admin"].delete(f"/projects/{pid}/members/{ids['analyst']}").status_code == 204
    with _runtime(app) as c:
        assert _count(c, "records", ids["analyst"], org) == 0


def test_writes_into_another_tenant_are_rejected(seeded):
    app, ids, pid = seeded["app"], seeded["ids"], seeded["pid"]
    outsider_org = _org_of(app, ids["outsider"])
    with _runtime(app) as c, pytest.raises(ProgrammingError, match="row-level security"):
        c.execute(text("SELECT set_config('app.user_id', :u, true), set_config('app.org_id', :o, true)"),
                  {"u": ids["outsider"], "o": outsider_org})
        c.execute(text("INSERT INTO documents (id, organisation_id, project_id, title, created_at, created_by) "
                       "VALUES ('doc-x', :o, :p, 'planted', now(), :u)"),
                  {"o": outsider_org, "p": pid, "u": ids["outsider"]})
    # Updates of invisible rows silently affect nothing.
    with _runtime(app) as c:
        c.execute(text("SELECT set_config('app.user_id', :u, true), set_config('app.org_id', :o, true)"),
                  {"u": ids["outsider"], "o": outsider_org})
        assert c.execute(text("UPDATE records SET status = 'approved'")).rowcount == 0


def test_audit_log_is_append_only_for_the_runtime_role(seeded):
    app, ids = seeded["app"], seeded["ids"]
    org = _org_of(app, ids["admin"])
    for sql in ("UPDATE audit_events SET action = 'tampered'", "DELETE FROM audit_events"):
        with _runtime(app) as c, pytest.raises(DBAPIError, match="permission denied"):
            c.execute(text("SELECT set_config('app.user_id', :u, true), set_config('app.org_id', :o, true)"),
                      {"u": ids["admin"], "o": org})
            c.execute(text(sql))


def test_context_does_not_leak_between_transactions(seeded):
    app, ids = seeded["app"], seeded["ids"]
    org = _org_of(app, ids["analyst"])
    with _runtime(app) as c:
        assert _count(c, "records", ids["analyst"], org) > 0
        c.commit()  # transaction-local settings end here
        assert c.execute(text("SELECT count(*) FROM records")).scalar() == 0


def test_app_layer_bypass_is_still_blocked_by_the_database(seeded):
    """Simulate a missing application check: look a record up by id with the outsider's context."""
    from ajgem.db import set_rls_context
    from ajgem.models import Record

    app, ids = seeded["app"], seeded["ids"]
    with app.state.admin_sessions() as s:
        rid = s.query(Record.id).first()[0]
    with app.state.sessions() as s:
        set_rls_context(s, user_id=ids["outsider"], org_id=_org_of(app, ids["outsider"]))
        assert s.get(Record, rid) is None
    with app.state.sessions() as s:
        set_rls_context(s, user_id=ids["analyst"], org_id=_org_of(app, ids["analyst"]))
        assert s.get(Record, rid) is not None
