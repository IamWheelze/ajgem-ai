"""Local development entry points.

    python -m ajgem serve                       # API on :8000 (/api + web client when frontend/dist is built)
    python -m ajgem create-user ORG EMAIL       # create an organisation (if new) and a user; prints the user id
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

    root = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
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

    if not argv or argv[0] not in {"serve", "create-user"}:
        print(__doc__)
        return 2
    app = create_app(Settings())
    if argv[0] == "serve":
        import uvicorn

        uvicorn.run(with_web_client(app), host="127.0.0.1", port=8000)
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
