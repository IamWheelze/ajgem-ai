"""Local development entry points.

    python -m ajgem serve                       # run the API on :8000
    python -m ajgem create-user ORG EMAIL       # create an organisation (if new) and a user; prints the user id
"""

from __future__ import annotations

import sys

from sqlalchemy import select


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

        uvicorn.run(app, host="127.0.0.1", port=8000)
        return 0
    org_name, email = argv[1], argv[2]
    with app.state.sessions() as s:
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
