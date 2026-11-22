from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from world_engine.api.app import create_app
from world_engine.api.credentials import CredentialRequest
from world_engine.api.security import LocalSession
from world_engine.application.credentials import AccountCredentialResolver, CredentialService
from world_engine.application.operations import ModelOperations, OperationsStore
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
    with TestClient(create_app(None, session, operations=ModelOperations(store, None), credentials=service)) as client:
        client.get("/api/v1/session")
        assert client.get(endpoint).json()["configured"] is False
        response = client.put(endpoint, json={"secret": value})
        assert response.status_code == 200 and value not in response.text
        ref = response.json()["secret_ref"]
        assert resolver.resolve(ref) == value
        assert client.get(endpoint).json() == {"configured": True, "backend": "windows_credential_manager"}
        assert value not in repr(CredentialRequest(secret=value))
        assert value not in str(store.list_providers(identity))

        def provider_request(document, **changes):
            fields = (
                "kind", "name", "base_url", "secret_ref", "enabled", "default_model",
                "max_retries", "capability_overrides", "allow_insecure_http",
            )
            return {key: document[key] for key in fields} | changes

        configured_provider = store.provider(identity, provider["provider_id"])
        same_origin_null_ref = provider_request(
            configured_provider, name="Cloud updated", base_url="https://api.deepseek.com/v2", secret_ref=None,
        )
        response = client.put(f"/api/v1/providers/{provider['provider_id']}", json=same_origin_null_ref)
        assert response.status_code == 200
        assert response.json()["secret_ref"] == ref
        assert client.get(endpoint).json()["configured"] is True
        assert resolver.resolve(ref) == value
        configured_provider = store.provider(identity, provider["provider_id"])

        # Provider edits cannot move a saved credential to a different endpoint,
        # even when the caller tries to clear or omit the reference in the PUT.
        changed_origin = provider_request(
            configured_provider, name="Changed", base_url="https://other.example/v1", secret_ref=None,
        )
        omitted_ref = provider_request(
            configured_provider, name="Changed", base_url="https://other.example/v1",
        )
        omitted_ref.pop("secret_ref")
        changed_kind = provider_request(configured_provider, kind="openai_compatible")
        changed_scheme = provider_request(configured_provider, base_url="http://localhost/v1")
        changed_port = provider_request(configured_provider, base_url="https://api.deepseek.com:444/v1")
        changed_reference = provider_request(configured_provider, secret_ref="env:OTHER_PROVIDER_KEY")
        trailing_dot_host = provider_request(
            configured_provider, base_url="https://api.deepseek.com./v2",
        )
        for payload in (
            changed_origin, omitted_ref, changed_kind, changed_scheme, changed_port, trailing_dot_host,
        ):
            response = client.put(f"/api/v1/providers/{provider['provider_id']}", json=payload)
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "PROVIDER_CREDENTIAL_DESTINATION_CONFLICT"
            assert response.json()["error"]["message"] == (
                "Remove the saved credential before changing the provider kind or endpoint origin."
            )
            assert value not in response.text
            assert store.provider(identity, provider["provider_id"]) == configured_provider
        response = client.put(
            f"/api/v1/providers/{provider['provider_id']}", json=changed_reference,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "PROVIDER_CREDENTIAL_REFERENCE_CONFLICT"
        assert value not in response.text
        assert store.provider(identity, provider["provider_id"]) == configured_provider

        # Host case and an explicit default HTTPS port normalize to the same
        # origin, so path, display name, and model edits remain valid.
        same_origin = provider_request(
            configured_provider,
            name="Cloud v2",
            base_url="https://API.DEEPSEEK.COM:443/v2",
            default_model="deepseek-next",
        )
        response = client.put(f"/api/v1/providers/{provider['provider_id']}", json=same_origin)
        assert response.status_code == 200
        assert response.json()["name"] == "Cloud v2"
        assert response.json()["base_url"] == "https://API.DEEPSEEK.COM:443/v2"
        assert response.json()["secret_ref"] == ref
        assert resolver.resolve(ref) == value

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

        # Deleting the saved credential releases the binding for a later edit.
        uncredentialed = store.provider(identity, provider["provider_id"])
        changed_after_delete = provider_request(
            uncredentialed, kind="openai_compatible", base_url="https://other.example/v1",
        )
        response = client.put(f"/api/v1/providers/{provider['provider_id']}", json=changed_after_delete)
        assert response.status_code == 200
        assert response.json()["kind"] == "openai_compatible"
        assert response.json()["base_url"] == "https://other.example/v1"
    # SQLite uses UTF-8 for serialized documents; no value entered this database.
    for file in tmp_path.iterdir():
        assert value.encode() not in file.read_bytes()
