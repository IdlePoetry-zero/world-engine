"""Account-scoped credential operations; no API ever returns a stored secret."""

from typing import Protocol

from world_engine.adapters.models.errors import ProviderError, ProviderErrorCode
from world_engine.domain.errors import Conflict
from world_engine.domain.models import AccessScope
from .operations import OperationsStore


class CredentialStore(Protocol):
    available: bool
    def set(self, target: str, value: str) -> None: ...
    def get(self, target: str) -> str | None: ...
    def delete(self, target: str) -> None: ...


class CredentialService:
    def __init__(self, operations: OperationsStore, credentials: CredentialStore):
        self.operations, self.credentials = operations, credentials

    def status(self, scope: AccessScope, provider_id: str):
        provider = self.operations.provider(scope, provider_id)
        target = self.operations.credential_target(scope, provider_id)
        configured = False
        if self.credentials.available and provider.get("secret_ref") == f"os:{target}":
            configured = bool(self.credentials.get(target))
        return {"configured": configured,
                "backend": "windows_credential_manager" if self.credentials.available else "unavailable"}

    def set(self, scope: AccessScope, provider_id: str, secret: str):
        provider = self.operations.provider(scope, provider_id)
        if not self.credentials.available:
            raise Conflict("OS credential storage is unavailable")
        if not secret or len(secret.encode("utf-8")) > 2560 or "\x00" in secret:
            raise Conflict("Credential length or format is invalid")
        target = self.operations.credential_target(scope, provider_id)
        self.credentials.set(target, secret)
        self.operations.save_provider(scope, provider | {"secret_ref": f"os:{target}"}, provider_id)
        return {"configured": True, "secret_ref": f"os:{target}"}

    def delete(self, scope: AccessScope, provider_id: str):
        provider = self.operations.provider(scope, provider_id)
        if not self.credentials.available:
            raise Conflict("OS credential storage is unavailable")
        target = self.operations.credential_target(scope, provider_id)
        self.credentials.delete(target)
        if provider.get("secret_ref") == f"os:{target}":
            self.operations.save_provider(scope, provider | {"secret_ref": None}, provider_id)


class AccountCredentialResolver:
    """A runtime resolver accepts only this account's exact provider references."""

    def __init__(self, service: CredentialService, identity: AccessScope):
        self.service, self.identity = service, identity

    def resolve(self, secret_ref: str) -> str:
        if not secret_ref.startswith("os:"):
            raise ProviderError(ProviderErrorCode.SECRET, "Unsupported secret reference scheme")
        providers = self.service.operations.list_providers(self.identity)
        for provider in providers:
            target = self.service.operations.credential_target(self.identity, provider["provider_id"])
            if secret_ref == f"os:{target}" and provider.get("secret_ref") == secret_ref:
                if not self.service.credentials.available:
                    break
                secret = self.service.credentials.get(target)
                if secret:
                    return secret
                break
        raise ProviderError(ProviderErrorCode.SECRET, "Referenced account credential is unavailable")
