"""ADR 0005/0008 的最小服务端会话与只追加审计底座。"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import re
import secrets
from typing import Literal

from fastapi import HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from superlily_contracts import canonicalize_json_value

from .models import (
    ControlPlaneAuditEvent,
    ControlPlaneLoginAttempt,
    ControlPlaneSession,
    new_id,
)
from .settings import ControlOperator, Settings
from .tool_invocation_service import database_now


CONTROL_SESSION_COOKIE = "__Host-superlily_control"
CONTROL_CSRF_HEADER = "X-CSRF-Token"
_SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_PASSWORD_VERIFY_LIMIT = asyncio.Semaphore(2)


class _ControlModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ControlLoginIn(_ControlModel):
    schema_version: Literal["1.0"] = "1.0"
    operator_id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z][a-z0-9_.-]+$")
    password: SecretStr = Field(min_length=12, max_length=1024)


class ControlReauthenticateIn(_ControlModel):
    schema_version: Literal["1.0"] = "1.0"
    password: SecretStr = Field(min_length=12, max_length=1024)


class ControlLogoutIn(_ControlModel):
    schema_version: Literal["1.0"] = "1.0"


@dataclass(frozen=True, slots=True)
class ControlSessionIdentity:
    session_id: str
    operator_id: str
    role: str
    resource_version: int
    issued_at: datetime
    expires_at: datetime
    last_reauthenticated_at: datetime


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_control_password(password: str, *, salt: bytes | None = None) -> str:
    """生成固定参数的 scrypt verifier；只供离线配置，不保存明文。"""

    if not 12 <= len(password) <= 1024:
        raise ValueError("control password must be between 12 and 1024 characters")
    active_salt = secrets.token_bytes(16) if salt is None else bytes(salt)
    if len(active_salt) != 16:
        raise ValueError("control password salt must be exactly 16 bytes")
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=active_salt,
        n=16_384,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt$16384$8$1${_b64url(active_salt)}${_b64url(derived)}"


def verify_control_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if (algorithm, n, r, p) != ("scrypt", "16384", "8", "1"):
            return False
        salt_bytes = _b64url_decode(salt)
        expected_bytes = _b64url_decode(expected)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt_bytes,
            n=16_384,
            r=8,
            p=1,
            dklen=32,
        )
        return secrets.compare_digest(actual, expected_bytes)
    except (UnicodeError, ValueError):
        return False


async def _verify_password_bounded(password: str, encoded: str) -> bool:
    async with _PASSWORD_VERIFY_LIMIT:
        return await asyncio.to_thread(verify_control_password, password, encoded)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fingerprint(settings: Settings, label: str, value: str) -> str:
    return hmac.new(
        settings.control_audit_pepper.encode("utf-8"),
        f"{label}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _control_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="control plane is not configured",
    )


def _control_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid control session",
    )


def _verify_boundary(
    request: Request,
    settings: Settings,
    *,
    require_origin: bool,
    require_json: bool,
) -> None:
    if not settings.control_operators:
        raise _control_unavailable()
    if request.headers.get("host") not in settings.control_allowed_hosts:
        raise HTTPException(status_code=403, detail="control Host is not allowed")
    if require_origin and request.headers.get("origin") not in settings.control_allowed_origins:
        raise HTTPException(status_code=403, detail="control Origin is not allowed")
    if require_json:
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise HTTPException(status_code=415, detail="control requests require application/json")


def verify_control_write_boundary(request: Request) -> None:
    """在 FastAPI 解析请求体前拒绝错误 authority/media type。"""

    _verify_boundary(
        request,
        request.app.state.settings,
        require_origin=True,
        require_json=True,
    )


def _client_host(request: Request) -> str:
    return "unknown" if request.client is None else request.client.host


async def append_control_audit(
    session: AsyncSession,
    *,
    event: str,
    outcome: Literal["accepted", "rejected"],
    reason_code: str,
    evidence: dict,
    identity: ControlSessionIdentity | None = None,
    operator: ControlOperator | None = None,
) -> None:
    canonical = canonicalize_json_value(evidence)
    session.add(
        ControlPlaneAuditEvent(
            id=new_id(),
            session_id=None if identity is None else identity.session_id,
            operator_id=(
                identity.operator_id
                if identity is not None
                else (None if operator is None else operator.operator_id)
            ),
            role=(
                identity.role
                if identity is not None
                else (None if operator is None else operator.role)
            ),
            event=event,
            outcome=outcome,
            reason_code=reason_code,
            evidence_json=canonical.value,
            evidence_hash=canonical.sha256,
        )
    )


def _identity(record: ControlPlaneSession) -> ControlSessionIdentity:
    return ControlSessionIdentity(
        session_id=record.id,
        operator_id=record.operator_id,
        role=record.role,
        resource_version=record.resource_version,
        issued_at=_aware(record.issued_at),
        expires_at=_aware(record.expires_at),
        last_reauthenticated_at=_aware(record.last_reauthenticated_at),
    )


def _session_view(identity: ControlSessionIdentity, settings: Settings, now: datetime) -> dict:
    return {
        "schema_version": "1.0",
        "operator_id": identity.operator_id,
        "role": identity.role,
        "resource_version": identity.resource_version,
        "issued_at": identity.issued_at,
        "expires_at": identity.expires_at,
        "reauthenticated_until": min(
            identity.expires_at,
            identity.last_reauthenticated_at
            + timedelta(seconds=settings.control_reauth_seconds),
        ),
        "fresh_reauthentication": (
            identity.last_reauthenticated_at
            + timedelta(seconds=settings.control_reauth_seconds)
            > now
        ),
    }


def _set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        CONTROL_SESSION_COOKIE,
        token,
        max_age=settings.control_session_seconds,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        CONTROL_SESSION_COOKIE,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


async def login_control_session(
    session: AsyncSession,
    request: Request,
    response: Response,
    payload: ControlLoginIn,
    settings: Settings,
) -> dict:
    _verify_boundary(request, settings, require_origin=True, require_json=True)
    now = _aware(await database_now(session))
    operator_lookup_hash = _fingerprint(settings, "operator", payload.operator_id)
    client_hash = _fingerprint(settings, "client", _client_host(request))
    cutoff = now - timedelta(seconds=settings.control_login_window_seconds)
    recent = await session.scalar(
        select(func.count(ControlPlaneLoginAttempt.id)).where(
            ControlPlaneLoginAttempt.created_at >= cutoff,
            ControlPlaneLoginAttempt.reason_code != "rate_limited",
            or_(
                ControlPlaneLoginAttempt.operator_lookup_hash == operator_lookup_hash,
                ControlPlaneLoginAttempt.client_fingerprint_hash == client_hash,
            ),
        )
    )
    operator = settings.control_operators.get(payload.operator_id)
    if int(recent or 0) >= settings.control_login_attempts:
        session.add(
            ControlPlaneLoginAttempt(
                id=new_id(),
                operator_lookup_hash=operator_lookup_hash,
                client_fingerprint_hash=client_hash,
                outcome="rejected",
                reason_code="rate_limited",
            )
        )
        await append_control_audit(
            session,
            event="session_login",
            outcome="rejected",
            reason_code="rate_limited",
            evidence={"operator_lookup_hash": operator_lookup_hash, "client_hash": client_hash},
            operator=operator,
        )
        await session.commit()
        raise HTTPException(status_code=429, detail="control login rate limited")

    verifier = operator.password_hash if operator is not None else next(
        item.password_hash for item in settings.control_operators.values()
    )
    valid = await _verify_password_bounded(
        payload.password.get_secret_value(),
        verifier,
    )
    if operator is None or not operator.enabled or not valid:
        session.add(
            ControlPlaneLoginAttempt(
                id=new_id(),
                operator_lookup_hash=operator_lookup_hash,
                client_fingerprint_hash=client_hash,
                outcome="rejected",
                reason_code="invalid_credentials",
            )
        )
        await append_control_audit(
            session,
            event="session_login",
            outcome="rejected",
            reason_code="invalid_credentials",
            evidence={"operator_lookup_hash": operator_lookup_hash, "client_hash": client_hash},
            operator=operator,
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="invalid control credentials")

    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    record = ControlPlaneSession(
        id=new_id(),
        token_hash=_sha256(token),
        csrf_hash=_sha256(csrf),
        operator_id=operator.operator_id,
        role=operator.role,
        resource_version=1,
        issued_at=now,
        expires_at=now + timedelta(seconds=settings.control_session_seconds),
        last_reauthenticated_at=now,
    )
    session.add(record)
    # PostgreSQL 必须先看见被审计事件引用的 session；仍在同一事务内，审计失败会整体回滚。
    await session.flush()
    session.add(
        ControlPlaneLoginAttempt(
            id=new_id(),
            operator_lookup_hash=operator_lookup_hash,
            client_fingerprint_hash=client_hash,
            outcome="accepted",
            reason_code="authenticated",
        )
    )
    identity = _identity(record)
    await append_control_audit(
        session,
        event="session_login",
        outcome="accepted",
        reason_code="authenticated",
        evidence={
            "session_id": record.id,
            "operator_lookup_hash": operator_lookup_hash,
            "client_hash": client_hash,
            "expires_at": record.expires_at.isoformat(),
        },
        identity=identity,
    )
    await session.commit()
    _set_session_cookie(response, token, settings)
    return {**_session_view(identity, settings, now), "csrf_token": csrf}


async def authenticate_control_session(
    session: AsyncSession,
    request: Request,
    settings: Settings,
    *,
    require_origin: bool,
    require_json: bool,
    require_csrf: bool,
    for_update: bool = False,
) -> tuple[ControlPlaneSession, ControlSessionIdentity, datetime]:
    _verify_boundary(
        request,
        settings,
        require_origin=require_origin,
        require_json=require_json,
    )
    token = request.cookies.get(CONTROL_SESSION_COOKIE, "")
    if not _SESSION_TOKEN_RE.fullmatch(token):
        raise _control_unauthorized()
    statement = select(ControlPlaneSession).where(
        ControlPlaneSession.token_hash == _sha256(token)
    )
    if for_update:
        statement = statement.with_for_update()
    record = await session.scalar(statement)
    now = _aware(await database_now(session))
    if record is None or record.revoked_at is not None or _aware(record.expires_at) <= now:
        raise _control_unauthorized()
    operator = settings.control_operators.get(record.operator_id)
    if operator is None or not operator.enabled or operator.role != record.role:
        raise _control_unauthorized()
    if require_csrf:
        supplied = request.headers.get(CONTROL_CSRF_HEADER, "")
        if not _SESSION_TOKEN_RE.fullmatch(supplied) or not secrets.compare_digest(
            record.csrf_hash,
            _sha256(supplied),
        ):
            raise HTTPException(status_code=403, detail="invalid control CSRF token")
    return record, _identity(record), now


async def current_control_session(
    session: AsyncSession,
    request: Request,
    settings: Settings,
) -> dict:
    _, identity, now = await authenticate_control_session(
        session,
        request,
        settings,
        require_origin=False,
        require_json=False,
        require_csrf=False,
    )
    return _session_view(identity, settings, now)


async def reauthenticate_control_session(
    session: AsyncSession,
    request: Request,
    payload: ControlReauthenticateIn,
    settings: Settings,
) -> dict:
    record, identity, now = await authenticate_control_session(
        session,
        request,
        settings,
        require_origin=True,
        require_json=True,
        require_csrf=True,
        for_update=True,
    )
    operator = settings.control_operators[identity.operator_id]
    valid = await _verify_password_bounded(
        payload.password.get_secret_value(),
        operator.password_hash,
    )
    if not valid:
        await append_control_audit(
            session,
            event="session_reauthenticate",
            outcome="rejected",
            reason_code="invalid_credentials",
            evidence={"session_version": identity.resource_version},
            identity=identity,
        )
        await session.commit()
        raise HTTPException(status_code=401, detail="invalid control credentials")
    csrf = secrets.token_urlsafe(32)
    next_version = identity.resource_version + 1
    changed = await session.execute(
        update(ControlPlaneSession)
        .where(
            ControlPlaneSession.id == identity.session_id,
            ControlPlaneSession.resource_version == identity.resource_version,
            ControlPlaneSession.csrf_hash == record.csrf_hash,
            ControlPlaneSession.revoked_at.is_(None),
        )
        .values(
            csrf_hash=_sha256(csrf),
            last_reauthenticated_at=now,
            resource_version=next_version,
            updated_at=now,
        )
    )
    if changed.rowcount != 1:
        await session.rollback()
        raise HTTPException(status_code=409, detail="control session changed")
    refreshed = ControlSessionIdentity(
        session_id=identity.session_id,
        operator_id=identity.operator_id,
        role=identity.role,
        resource_version=next_version,
        issued_at=identity.issued_at,
        expires_at=identity.expires_at,
        last_reauthenticated_at=now,
    )
    await append_control_audit(
        session,
        event="session_reauthenticate",
        outcome="accepted",
        reason_code="reauthenticated",
        evidence={
            "previous_version": identity.resource_version,
            "resource_version": refreshed.resource_version,
        },
        identity=refreshed,
    )
    await session.commit()
    return {**_session_view(refreshed, settings, now), "csrf_token": csrf}


async def logout_control_session(
    session: AsyncSession,
    request: Request,
    response: Response,
    settings: Settings,
) -> dict:
    record, identity, now = await authenticate_control_session(
        session,
        request,
        settings,
        require_origin=True,
        require_json=True,
        require_csrf=True,
        for_update=True,
    )
    next_version = identity.resource_version + 1
    changed = await session.execute(
        update(ControlPlaneSession)
        .where(
            ControlPlaneSession.id == identity.session_id,
            ControlPlaneSession.resource_version == identity.resource_version,
            ControlPlaneSession.csrf_hash == record.csrf_hash,
            ControlPlaneSession.revoked_at.is_(None),
        )
        .values(
            revoked_at=now,
            resource_version=next_version,
            updated_at=now,
        )
    )
    if changed.rowcount != 1:
        await session.rollback()
        raise HTTPException(status_code=409, detail="control session changed")
    revoked_identity = ControlSessionIdentity(
        session_id=identity.session_id,
        operator_id=identity.operator_id,
        role=identity.role,
        resource_version=next_version,
        issued_at=identity.issued_at,
        expires_at=identity.expires_at,
        last_reauthenticated_at=identity.last_reauthenticated_at,
    )
    await append_control_audit(
        session,
        event="session_logout",
        outcome="accepted",
        reason_code="revoked",
        evidence={
            "previous_version": identity.resource_version,
            "resource_version": next_version,
        },
        identity=revoked_identity,
    )
    await session.commit()
    _clear_session_cookie(response)
    return {"schema_version": "1.0", "revoked": True}
