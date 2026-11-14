from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from world_engine.api.app import create_app
from world_engine.api.credentials import CredentialRequest
from world_engine.api.security import LocalSession
from world_engine.application.credentials import AccountCredentialResolver, CredentialService
from world_engine.application.operations import OperationsStore
from world_engine.adapters.models.errors import ProviderError
from world_engine.domain.models import AccessScope


class MemoryCredentialFixture:
    available = True

    def __init__(self):
        self.values = {}

    def set(self, target, value):
        self.values[target] = value

    def get(self, target):
        return self.values.get(target)

    def delete(self, target):
        self.values.pop(target, None)


def test_secret_write_only_api_and_account_bound_resolver(tmp_path):
    identity = AccessScope(tenant_id=uuid4(), user_id=uuid4())
    store = OperationsStore(tmp_path / "ops.db")
    provider = store.save_provider(identity, {"kind": "deepseek", "name": "Cloud", "base_url": "https://api.deepseek.com"})
    credentials = MemoryCredentialFixture()
    service = CredentialService(store, credentials)
    resolver = AccountCredentialResolver(service, identity)
    session = LocalSession(str(identity.user_id), str(identity.tenant_id), frozenset({"http://testserver"}))
    value = "synthetic-test-value-" + str(uuid4())
    endpoint = f"/api/v1/providers/{provider['provider_id']}/credential"
    with TestClient(create_app(None, session, credentials=service)) as client:
        client.get("/api/v1/session")
        assert client.get(endpoint).json()["configured"] is False
        response = client.put(endpoint, json={"secret": value})
        assert response.status_code == 200 and value not in response.text
        ref = response.json()["secret_ref"]
        assert resolver.resolve(ref) == value
        assert client.get(endpoint).json() == {"configured": True, "backend": "windows_credential_manager"}
        assert value not in repr(CredentialRequest(secret=value))
        assert value not in str(store.list_providers(identity))
        stranger = AccessScope(tenant_id=identity.tenant_id, user_id=uuid4())
        with pytest.raises(ProviderError):
            AccountCredentialResolver(service, stranger).resolve(ref)
        with pytest.raises(ValueError):
            store.save_provider(identity, provider | {"secret_ref": ref + "foreign"}, provider["provider_id"])
        invalid = client.put(endpoint, json={"secret": value, "extra_secret": value})
        assert invalid.status_code == 422 and value not in invalid.text
        assert client.delete(endpoint).status_code == 204
        assert client.get(endpoint).json()["configured"] is False
        with pytest.raises(ProviderError):
            resolver.resolve(ref)
    # SQLite uses UTF-8 for serialized documents; no value entered this database.
    for file in tmp_path.iterdir():
        assert value.encode() not in file.read_bytes()
