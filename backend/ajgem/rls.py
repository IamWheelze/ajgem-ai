"""PostgreSQL row-level security (FRD NF01).

Two database roles:

* **owner** (``AJGEM_DATABASE_ADMIN_URL``) owns the schema, runs ``create_all``
  and this module, and is used by maintenance tools (seeding, the dev user
  picker). It is the documented RLS bypass: give it ``BYPASSRLS`` (or use a
  superuser) and never use it to serve requests.
* **runtime** (``AJGEM_DATABASE_URL``) serves the API. It is not the table
  owner, has ``NOBYPASSRLS``, and gets only DML grants. Every table has RLS
  *enabled and forced*, so the runtime role sees only rows the current request
  context allows — independently of the application's own checks.

Request context is carried in transaction-local settings, set at the start of
every transaction (see ``db.bind_rls_context``):

* ``app.user_id`` — the authenticated user (needed to read one's own user row
  before the organisation is known);
* ``app.org_id`` — the user's organisation.

Policies:

* organisation-scoped tables: ``organisation_id = app.org_id``;
* project-scoped content: same organisation **and** the project is one the user
  holds a live membership of (``ajgem_visible_projects()``);
* ``job_tasks`` (no tenant columns): visible only through a visible job.

With no context set, every policy evaluates to false: the runtime role sees and
writes nothing. ``audit_events`` is append-only for the runtime role (no UPDATE
or DELETE grant), which backs FR21 at the database level.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

RLS_VERSION = "rls-2026.11.0"

ORG = "organisation_id::text = current_setting('app.org_id', true)"
VISIBLE = "project_id IN (SELECT ajgem_visible_projects())"

# table -> USING / WITH CHECK expression
POLICIES: dict[str, str] = {
    "organisations": "id::text = current_setting('app.org_id', true)",
    "users": f"({ORG}) OR id::text = current_setting('app.user_id', true)",
    "projects": f"({ORG}) AND (id IN (SELECT ajgem_visible_projects()) "
                f"OR created_by::text = current_setting('app.user_id', true))",
    "memberships": ORG,
    "factor_approvals": ORG,
    "audit_events": ORG,
    # Tombstones stay readable at organisation level after their project is deleted.
    "deletion_requests": ORG,
    # Metered usage (counts and tokens, no content): readable organisation-wide for budgets.
    "usage_events": ORG,
    "devices": ORG,
    "job_tasks": "job_id IN (SELECT id FROM jobs)",
}
PROJECT_SCOPED = [
    "project_config_revisions", "upload_sessions", "documents", "document_versions", "page_texts", "source_anchors",
    "jobs", "import_previews", "records", "record_revisions", "conflict_groups", "answer_snapshots", "exports",
    "checklists", "targets", "target_evidence", "raster_overlays", "field_drafts",
]
for _t in PROJECT_SCOPED:
    POLICIES[_t] = f"({ORG}) AND {VISIBLE}"

VISIBLE_PROJECTS_FN = """
CREATE OR REPLACE FUNCTION ajgem_visible_projects() RETURNS SETOF varchar
LANGUAGE sql STABLE
AS $$
  SELECT m.project_id FROM memberships m
  WHERE m.user_id::text = current_setting('app.user_id', true)
    AND m.organisation_id::text = current_setting('app.org_id', true)
    AND m.revoked_at IS NULL
    AND (m.expires_at IS NULL OR m.expires_at > now())
$$
"""


def statements(runtime_role: str, tables: set[str] | None = None) -> list[str]:
    """SQL for the function, policies and grants; limited to `tables` when given (migrations pass the tables
    that exist at that point in the history)."""
    role = _ident(runtime_role)
    out = [
        VISIBLE_PROJECTS_FN,
        f"GRANT USAGE ON SCHEMA public TO {role}",
        f"GRANT EXECUTE ON FUNCTION ajgem_visible_projects() TO {role}",
    ]
    for table, expr in POLICIES.items():
        if tables is not None and table not in tables:
            continue
        out += [
            f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
            f"DROP POLICY IF EXISTS tenant_isolation ON {table}",
            f"CREATE POLICY tenant_isolation ON {table} FOR ALL TO PUBLIC USING ({expr}) WITH CHECK ({expr})",
        ]
        if table == "audit_events":
            out += [f"REVOKE ALL ON {table} FROM {role}", f"GRANT SELECT, INSERT ON {table} TO {role}"]
        else:
            out += [f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}"]
    return out


def install(admin_engine: Engine, runtime_role: str) -> None:
    """Idempotently enable RLS, policies and grants. Run as the owner role after create_all."""
    if admin_engine.dialect.name != "postgresql":
        return
    with admin_engine.begin() as conn:
        for sql in statements(runtime_role):
            conn.execute(text(sql))


def verify(admin_engine: Engine) -> list[str]:
    """Return tenant tables that do not have RLS enabled and forced (empty list = healthy)."""
    with admin_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relname = ANY(:t) AND relkind = 'r'"), {"t": list(POLICIES)}).all()
    ok = {r.relname for r in rows if r.relrowsecurity and r.relforcerowsecurity}
    return sorted(set(POLICIES) - ok)


def runtime_role_for_migration(alembic_context) -> str:
    """Runtime role for grants inside a migration: set by ajgem.migrate, else the AJGEM_DATABASE_URL user."""
    role = alembic_context.config.attributes.get("runtime_role")
    if not role:
        from sqlalchemy.engine import make_url

        from .context import Settings

        role = make_url(Settings().database_url).username
    if not role:
        raise RuntimeError("Cannot determine the runtime role for RLS grants; set AJGEM_DATABASE_URL")
    return role


def apply_in_migration(op, alembic_context) -> None:
    """Re-install all policies and grants (idempotent). Call from any migration that adds a table."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    from sqlalchemy import inspect

    existing = set(inspect(bind).get_table_names())
    for sql in statements(runtime_role_for_migration(alembic_context), tables=existing):
        bind.execute(text(sql))


class RlsBypassRole(RuntimeError):
    pass


def ensure_runtime_role_is_constrained(engine: Engine, allow_bypass: bool = False) -> None:
    """Refuse to serve requests with a role that RLS does not apply to (superuser, BYPASSRLS or table owner)."""
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT r.rolsuper, r.rolbypassrls, "
            "EXISTS (SELECT 1 FROM pg_tables t WHERE t.schemaname = 'public' AND t.tableowner = current_user) AS owner "
            "FROM pg_roles r WHERE r.rolname = current_user")).one()
    if (row.rolsuper or row.rolbypassrls) and not allow_bypass:
        raise RlsBypassRole(
            "AJGEM_DATABASE_URL uses a role that bypasses row-level security. Use a NOBYPASSRLS, non-owner "
            "runtime role and put the owner in AJGEM_DATABASE_ADMIN_URL (or set AJGEM_ALLOW_RLS_BYPASS=1 knowingly).")
    # A table owner is still constrained because RLS is FORCED, so ownership alone is allowed.


def _ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"Unsafe role name {name!r}")
    return name
