import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True, slots=True)
class InvocationIdentity:
    caller: Literal["command", "admin_api"]
    subject: str


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def ingest_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    for instance_id, expected in request.app.state.settings.ingest_tokens.items():
        if expected and secrets.compare_digest(credentials.credentials, expected):
            return instance_id
    raise _unauthorized()


async def provider_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    for provider_id, expected in request.app.state.settings.provider_tokens.items():
        if expected and secrets.compare_digest(credentials.credentials, expected):
            return provider_id
    raise _unauthorized()


async def model_provider_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    for provider_id, expected in request.app.state.settings.model_provider_tokens.items():
        if expected and secrets.compare_digest(credentials.credentials, expected):
            return provider_id
    raise _unauthorized()


async def invocation_identity(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> InvocationIdentity:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    supplied = credentials.credentials
    expected_admin = request.app.state.settings.admin_token
    if expected_admin and secrets.compare_digest(supplied, expected_admin):
        return InvocationIdentity(caller="admin_api", subject="core-admin")
    for instance_id, expected in request.app.state.settings.ingest_tokens.items():
        if expected and secrets.compare_digest(supplied, expected):
            return InvocationIdentity(caller="command", subject=instance_id)
    raise _unauthorized()


async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    expected = request.app.state.settings.admin_token
    if (
        credentials is None
        or credentials.scheme.lower() != "bearer"
        or not expected
        or not secrets.compare_digest(credentials.credentials, expected)
    ):
        raise _unauthorized()
