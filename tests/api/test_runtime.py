from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from world_engine.runtime import build_application, local_identity


class EmptyTestCatalog:
    def list_packs(self):
        return []


def test_identity_publication_is_atomic_and_corruption_is_not_replaced(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        identities = list(pool.map(lambda _: local_identity(tmp_path), range(20)))
    assert len({(item.tenant_id, item.user_id) for item in identities}) == 1
    assert list(tmp_path.glob("*.tmp")) == []
    target = tmp_path / "local-identity.json"
    target.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        local_identity(tmp_path)
    assert target.read_text() == "broken"


def test_runtime_applies_migrations_and_preserves_configuration(tmp_path, monkeypatch):
    monkeypatch.delenv("WORLD_ENGINE_DATABASE_URL", raising=False)
    app = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        identity = client.get("/api/v1/session").json()
        providers = client.get("/api/v1/providers").json()["items"]
        assert {p["provider_id"] for p in providers} == {"deepseek", "ollama"}
        assert client.get("/api/v1/worlds").json() == {"items": []}
        assert client.put("/api/v1/settings", json={"local_only": True, "routes": {}}).status_code == 200
    with sqlite3.connect(tmp_path / "world-engine.db") as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone()
    second = build_application(data_directory=tmp_path, catalog=EmptyTestCatalog())
    with TestClient(second, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/v1/session").json() == identity
        settings = client.get("/api/v1/settings").json()
        assert settings["local_only"] and settings["routes"] == {}
        assert len(client.get("/api/v1/providers").json()["items"]) == 2
    # Only opaque IDs are stored in the identity document, never session cookies.
    assert set(json.loads((tmp_path / "local-identity.json").read_text())) == {"tenant_id", "user_id"}
