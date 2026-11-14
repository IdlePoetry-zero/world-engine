from fastapi import APIRouter, Request, Response
from pydantic import Field, SecretStr

from world_engine.application.credentials import CredentialService
from .contracts import RequestModel


class CredentialRequest(RequestModel):
    secret: SecretStr = Field(min_length=1, max_length=2560)


def credential_router(service: CredentialService, scope):
    router = APIRouter(prefix="/api/v1/providers")

    @router.get("/{provider_id}/credential")
    def status(request: Request, provider_id: str):
        return service.status(scope(request), provider_id)

    @router.put("/{provider_id}/credential")
    def save(request: Request, provider_id: str, body: CredentialRequest):
        return service.set(scope(request), provider_id, body.secret.get_secret_value())

    @router.delete("/{provider_id}/credential", status_code=204)
    def delete(request: Request, provider_id: str):
        service.delete(scope(request), provider_id)
        return Response(status_code=204)

    return router
