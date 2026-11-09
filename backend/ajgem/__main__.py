"""Local development entry points.

    python -m ajgem serve                       # API on :8000 (/api + web client when frontend/dist is built)
    python -m ajgem worker [--once]             # process queued jobs (AJGEM_JOB_EXECUTOR=worker); run one or more
    python -m ajgem create-user ORG EMAIL       # create an organisation (if new) and a user; prints the user id
    python -m ajgem migrate                     # apply pending migrations (owner role); --stamp REV adopts a DB
    python -m ajgem db-status                   # show the database revision and the latest available
    python -m ajgem revoke-sessions EMAIL       # refuse every sign-in token issued to EMAIL before now (NF02)
    python -m ajgem verify [--json]             # check sources, anchors, records and grants (run after a restore, NF06)
    python -m ajgem set-budget ORG [--pages N] [--model-tokens N]   # monthly organisation budget (NF11); "none" clears
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select


WEB_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def with_web_client(api_app):
    """Serve the built React client at / and the API under /api, if frontend/dist exists."""
    if not WEB_DIST.is_dir():
        return api_app
    from fastapi import FastAPI
    from fastapi.responses import FileResponse

    from . import security

    root = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    security.install(root, api_app.state.ctx.settings, api=False)
    root.mount("/api", api_app)

    @root.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = (WEB_DIST / path).resolve()
        if path and f.is_file() and WEB_DIST in f.parents:
            return FileResponse(f)
        return FileResponse(WEB_DIST / "index.html")

    return root


def main(argv: list[str]) -> int:
    from .api.app import create_app
    from .context import Settings
    from .models import Organisation, User

    if not argv or argv[0] not in {"serve", "worker", "create-user", "migrate", "db-status", "revoke-sessions", "set-budget", "verify"}:
        print(__doc__)
        return 2
    if argv[0] in {"migrate", "db-status"}:
        from . import migrate
        from .db import make_engine

        settings = Settings()
        admin = make_engine(settings.database_admin_url or settings.database_url)
        runtime_role = make_engine(settings.database_url).url.username
        if argv[0] == "db-status":
            print(f"database: {migrate.current_revision(admin) or 'none'}  latest: {migrate.head_revision()}")
        elif len(argv) == 3 and argv[1] == "--stamp":
            migrate.stamp(admin, argv[2])
            print(f"stamped {argv[2]}")
        else:
            migrate.upgrade(admin, runtime_role)
            print(f"at {migrate.current_revision(admin)}")
        return 0
    app = create_app(Settings())
    if argv[0] == "serve":
        import uvicorn

        uvicorn.run(with_web_client(app), host="127.0.0.1", port=8000)
        return 0
    if argv[0] == "worker":
        import logging

        from .services import worker

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        if "--once" in argv:
            print(worker.run_once(app.state) or "queue empty")
        else:
            worker.run_forever(app.state)
        return 0
    if argv[0] == "verify":
        import json

        from .services import integrity

        with app.state.admin_sessions() as s:  # owner role: the checks span organisations; read-only
            report = integrity.verify(s, app.state.ctx.store).as_dict()
        if "--json" in argv:
            print(json.dumps(report, indent=2))
        else:
            for k, v in report["checked"].items():
                print(f"  {k:24} {v}")
            for code, p in report["problems"].items():
                print(f"PROBLEM {code}: {p['count']} (e.g. {', '.join(p['examples'][:3])})")
            print("OK: all checks passed" if report["ok"] else "FAILED: see problems above")
        return 0 if report["ok"] else 1
    if argv[0] == "set-budget":
        if len(argv) < 2:
            print(__doc__)
            return 2
        flags = {"--pages": "monthly_pages", "--model-tokens": "monthly_model_tokens"}
        with app.state.admin_sessions() as s:  # owner role: the organisation's budget is an operator setting
            org = s.scalar(select(Organisation).where(Organisation.name == argv[1]))
            if org is None:
                print(f"no organisation {argv[1]}")
                return 1
            budget = dict(org.budget or {})
            rest = argv[2:]
            for flag, value in zip(rest[::2], rest[1::2]):
                if flag not in flags:
                    print(f"unknown option {flag}")
                    return 2
                if value.lower() == "none":
                    budget.pop(flags[flag], None)
                elif value.isdigit():
                    budget[flags[flag]] = int(value)
                else:
                    print(f"{flag} needs a whole number or 'none'")
                    return 2
            from .errors import ApiError
            from .services.costs import validate_limits

            try:
                org.budget = validate_limits(budget)
            except ApiError as e:
                print(e.message)
                return 2
            s.commit()
            print(f"{org.name}: {org.budget or 'no budget limits'}")
        return 0
    if argv[0] == "revoke-sessions":
        from datetime import datetime, timezone

        with app.state.admin_sessions() as s:  # owner role: user lookup across organisations
            user = s.scalar(select(User).where(User.email == argv[1]))
            if user is None:
                print(f"no user {argv[1]}")
                return 1
            user.sessions_revoked_before = datetime.now(timezone.utc)
            s.commit()
            print(f"sessions revoked for {argv[1]}")
        return 0
    org_name, email = argv[1], argv[2]
    with app.state.admin_sessions() as s:  # owner role: creating organisations and users bypasses RLS
        org = s.scalar(select(Organisation).where(Organisation.name == org_name)) or Organisation(name=org_name)
        s.add(org)
        s.flush()
        user = User(organisation_id=org.id, email=email, name=email.split("@")[0])
        s.add(user)
        s.commit()
        print(user.id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
