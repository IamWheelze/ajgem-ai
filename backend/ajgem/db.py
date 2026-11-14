"""Database engine, session factory and shared column types.

PostgreSQL (with PostGIS) is the production target (FRD §9). SQLite is
supported for local development and the test suite. Decimal values are stored
as NUMERIC on PostgreSQL and as exact text on SQLite, so no measurement ever
passes through a float.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import JSON, Numeric, String, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


PACK_MIN = 32  # lists shorter than this are stored as they are


def pack_lists(doc):
    """Store long lists of dicts as shapes + values instead of repeating every key in every item (NF09).

    ``{"rows": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}`` becomes
    ``{"rows": {"__packed__": "shapes-v1", "shapes": [["a", "b"]], "data": [[0, 1, 2], [0, 3, 4]]}}``.
    Item order and each item's exact keys are kept; values are not touched. Only top-level lists are packed.
    """
    if not isinstance(doc, dict):
        return doc
    out = {}
    for k, v in doc.items():
        if isinstance(v, list) and len(v) >= PACK_MIN and all(type(x) is dict for x in v):
            shapes: dict[tuple, int] = {}
            data = []
            append = data.append
            last_keys, last_sid = None, 0
            for item in v:
                keys = tuple(item)
                # Rows almost always share one shape. Comparing with the last one (ordered, and fast because the
                # key strings are the same objects) avoids hashing every row's keys.
                if keys != last_keys:
                    last_keys = keys
                    last_sid = shapes.setdefault(keys, len(shapes))
                append([last_sid, *item.values()])
            out[k] = {"__packed__": "shapes-v1", "shapes": [list(t) for t in shapes], "data": data}
        else:
            out[k] = v
    return out


def unpack_lists(doc):
    if not isinstance(doc, dict):
        return doc
    out = {}
    for k, v in doc.items():
        if isinstance(v, dict) and v.get("__packed__") == "shapes-v1":
            shapes = v["shapes"]
            out[k] = [dict(zip(shapes[row[0]], row[1:], strict=True)) for row in v["data"]]
        else:
            out[k] = v
    return out


class PackedJSON(TypeDecorator):
    """JSON whose long top-level lists of dicts are stored compactly; readers always see plain dicts."""

    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return pack_lists(value)

    def process_result_value(self, value, dialect):
        return unpack_lists(value)


class ExactDecimal(TypeDecorator):
    """Exact decimal storage: NUMERIC on PostgreSQL, canonical text elsewhere."""

    impl = String(64)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(Numeric(asdecimal=True))
        return dialect.type_descriptor(String(64))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = Decimal(value)
        return value if dialect.name == "postgresql" else str(value)

    def process_result_value(self, value, dialect):
        return None if value is None else Decimal(value)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}  # noqa: RUF012 - read by SQLAlchemy, never mutated


def _compact_json(value) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


def make_engine(url: str) -> Engine:
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
    # Compact JSON: answer snapshots can hold 100,000 rows, and the default ", " / ": " separators add about a tenth
    # to what is sent and stored (NF09).
    kwargs["json_serializer"] = _compact_json
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


# ---------------------------------------------------------------- RLS request context
RLS_KEYS = {"user_id": "app.user_id", "org_id": "app.org_id"}


def _apply_rls(connection, values: dict) -> None:
    if connection.dialect.name != "postgresql":
        return
    from sqlalchemy import text

    for key, setting in RLS_KEYS.items():
        # Transaction-local (is_local = true): the setting ends with the transaction.
        connection.execute(text("SELECT set_config(:k, :v, true)"), {"k": setting, "v": values.get(key) or ""})


def set_rls_context(session, **values) -> None:
    """Record the request identity for RLS and apply it to the current transaction, if any."""
    ctx = session.info.setdefault("rls", {})
    ctx.update({k: v for k, v in values.items() if k in RLS_KEYS})
    if session.in_transaction():
        _apply_rls(session.connection(), ctx)


def bind_rls_context(session_factory: sessionmaker) -> None:
    """Re-apply the stored context at the start of every transaction (commits end transaction-local settings)."""

    @event.listens_for(session_factory, "after_begin")
    def _after_begin(session, transaction, connection):  # pragma: no cover - exercised on PostgreSQL
        ctx = session.info.get("rls")
        if ctx:
            _apply_rls(connection, ctx)
