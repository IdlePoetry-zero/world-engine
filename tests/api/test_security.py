from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from world_engine.api.security import LocalSession, LocalSessionMiddleware


def make_client():
    app = FastAPI()
    session = LocalSession("local-user", "local-tenant", frozenset({"http://testserver"}))
    app.add_middleware(LocalSessionMiddleware, session=session)

    @app.get("/api/v1/session")
    def bootstrap(request: Request):
        return {"id": request.state.principal_id}

    @app.post("/api/v1/commands")
    def command(request: Request):
        return {"id": request.state.principal_id, "tenant": request.state.tenant_id}

    return TestClient(app), session


def test_api_requires_bootstrap_and_uses_server_identity():
    client, session = make_client()
    assert client.post("/api/v1/commands").status_code == 401
    response = client.get("/api/v1/session")
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert session.token not in response.text
    response = client.post("/api/v1/commands", json={"principal_id": "other", "tenant_id": "other"})
    assert response.json() == {"id": "local-user", "tenant": "local-tenant"}
    assert response.headers["cache-control"] == "no-store"


def test_other_website_cannot_bootstrap_or_mutate_with_existing_cookie():
    client, _ = make_client()
    assert client.get("/api/v1/session", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/v1/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    client.get("/api/v1/session")
    assert client.post("/api/v1/commands", headers={"Origin": "null"}).status_code == 403
    assert client.post("/api/v1/commands", headers={"Origin": "http://testserver.evil.example"}).status_code == 403


def test_host_is_exact_and_forged_cookie_rejected():
    client, session = make_client()
    assert client.get("/api/v1/session", headers={"Host": "attacker.example"}).status_code == 400
    client.cookies.set(session.cookie_name, "forged")
    assert client.post("/api/v1/commands").status_code == 401
    assert session.token not in repr(session)
