import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer = HTTPBearer(auto_error=False)


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

