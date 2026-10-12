"""Phase 0 HTTP smoke test (non-destructive).

Exercises the real ASGI app + auth stack against real data without creating any
records: mints a JWT for an existing user and calls protected endpoints.

Run: PYTHONPATH=. .venv/bin/python scripts/test_api.py
"""

import asyncio

import httpx

from app.core.security import create_access_token
from app.db import close_db, init_db
from app.main import app
from app.models.user import User


async def main() -> None:
    await init_db()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Health
        r = await client.get("/health")
        print("GET /health ->", r.status_code, r.json())
        assert r.status_code == 200

        # Pick a real, well-formed user.
        user = await User.find_one({"email": {"$regex": ".+@.+"}})
        assert user is not None, "no real user found"
        token = create_access_token(str(user.id))
        print("minted token for:", user.email)

        # Protected /me with a valid token
        r = await client.get(
            "/api/v1/user/me", headers={"Authorization": f"Bearer {token}"}
        )
        print("GET /user/me ->", r.status_code)
        body = r.json()
        assert r.status_code == 200 and body["success"] is True
        assert body["data"]["email"] == user.email
        assert "password" not in body["data"]
        print("   -> ok, email matches, no password leaked")

        # Token via query string (EventSource path)
        r = await client.get(f"/api/v1/user/me?token={token}")
        print("GET /user/me?token= ->", r.status_code)
        assert r.status_code == 200

        # Missing token -> 401 with legacy envelope
        r = await client.get("/api/v1/user/me")
        print("GET /user/me (no token) ->", r.status_code, r.json())
        assert r.status_code == 401 and r.json()["success"] is False

        # Bad token -> 401
        r = await client.get(
            "/api/v1/user/me", headers={"Authorization": "Bearer garbage.token.x"}
        )
        print("GET /user/me (bad token) ->", r.status_code)
        assert r.status_code == 401

        # Login with wrong password -> 401 envelope
        r = await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "definitely-wrong"}
        )
        print("POST /auth/login (wrong pw) ->", r.status_code, r.json())
        assert r.status_code == 401 and r.json()["success"] is False

    print("\nHTTP SMOKE TEST PASSED ✅")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
