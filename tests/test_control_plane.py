from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import json

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError

from superlily_core.control_plane import (
    CONTROL_CSRF_HEADER,
    CONTROL_SESSION_COOKIE,
    hash_control_password,
    verify_control_password,
)
from superlily_core.models import (
    ControlPlaneAuditEvent,
    ControlPlaneLoginAttempt,
    ControlPlaneSession,
)
from superlily_core.settings import ControlOperator


CONTROL_PASSWORD = "correct horse battery staple"
CONTROL_PASSWORD_HASH = hash_control_password(CONTROL_PASSWORD, salt=b"0123456789abcdef")
CONTROL_ORIGIN = "https://control.test"


def _enable_control(app, *, login_attempts: int = 5) -> None:
    app.state.settings = replace(
        app.state.settings,
        control_operators={
            "reviewer.one": ControlOperator(
                operator_id="reviewer.one",
                role="reviewer",
                password_hash=CONTROL_PASSWORD_HASH,
            )
        },
        control_allowed_hosts=frozenset({"control.test"}),
        control_allowed_origins=frozenset({CONTROL_ORIGIN}),
        control_audit_pepper="control-test-audit-pepper-32-bytes-minimum",
        control_session_seconds=900,
        control_reauth_seconds=300,
        control_login_attempts=login_attempts,
        control_login_window_seconds=300,
    )


def _login_payload(*, password: str = CONTROL_PASSWORD, operator_id: str = "reviewer.one") -> dict:
    return {
        "schema_version": "1.0",
        "operator_id": operator_id,
        "password": password,
    }


def _control_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=CONTROL_ORIGIN,
    )


def test_control_password_verifier_is_bounded_and_does_not_echo_password() -> None:
    assert CONTROL_PASSWORD not in CONTROL_PASSWORD_HASH
    assert verify_control_password(CONTROL_PASSWORD, CONTROL_PASSWORD_HASH) is True
    assert verify_control_password("wrong password", CONTROL_PASSWORD_HASH) is False
    assert verify_control_password(CONTROL_PASSWORD, "scrypt$2$8$1$bad$bad") is False
    with pytest.raises(ValueError, match="between 12 and 1024"):
        hash_control_password("too short")


async def test_control_plane_is_disabled_without_operator_configuration(client) -> None:
    response = await client.post(
        "/v1/control/session/login",
        json=_login_payload(),
        headers={"origin": CONTROL_ORIGIN},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "control plane is not configured"}


async def test_login_requires_exact_host_origin_and_json_content_type(app) -> None:
    _enable_control(app)
    async with _control_client(app) as client:
        wrong_host = await client.post(
            "/v1/control/session/login",
            json=_login_payload(),
            headers={"host": "evil.test", "origin": CONTROL_ORIGIN},
        )
        wrong_origin = await client.post(
            "/v1/control/session/login",
            json=_login_payload(),
            headers={"origin": "https://evil.test"},
        )
        wrong_content_type = await client.post(
            "/v1/control/session/login",
            content=json.dumps(_login_payload()),
            headers={"origin": CONTROL_ORIGIN, "content-type": "text/plain"},
        )
        leaked_password = "must-never-echo"
        invalid_body = await client.post(
            "/v1/control/session/login",
            json=_login_payload(password=leaked_password) | {"unexpected": True},
            headers={"origin": CONTROL_ORIGIN},
        )

    assert wrong_host.status_code == 403
    assert wrong_origin.status_code == 403
    assert wrong_content_type.status_code == 415
    assert invalid_body.status_code == 422
    assert invalid_body.json() == {"detail": "invalid control request"}
    assert leaked_password not in invalid_body.text
    for response in (wrong_host, wrong_origin, wrong_content_type, invalid_body):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == (
            "default-src 'none'; frame-ancestors 'none'"
        )


async def test_control_session_lifecycle_rotates_csrf_and_never_persists_secrets(app) -> None:
    _enable_control(app)
    async with _control_client(app) as client:
        rejected = await client.post(
            "/v1/control/session/login",
            json=_login_payload(password="wrong password value"),
            headers={"origin": CONTROL_ORIGIN},
        )
        assert rejected.status_code == 401

        login = await client.post(
            "/v1/control/session/login",
            json=_login_payload(),
            headers={"origin": CONTROL_ORIGIN},
        )
        assert login.status_code == 200
        assert login.json()["operator_id"] == "reviewer.one"
        assert login.json()["role"] == "reviewer"
        assert login.json()["resource_version"] == 1
        csrf = login.json()["csrf_token"]
        cookie = client.cookies.get(CONTROL_SESSION_COOKIE)
        assert cookie is not None
        set_cookie = login.headers["set-cookie"]
        assert "Secure" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=strict" in set_cookie
        assert "Path=/" in set_cookie
        assert login.headers["cache-control"] == "no-store"

        current = await client.get("/v1/control/session")
        assert current.status_code == 200
        assert "csrf_token" not in current.json()

        missing_csrf = await client.post(
            "/v1/control/session/reauthenticate",
            json={"schema_version": "1.0", "password": CONTROL_PASSWORD},
            headers={"origin": CONTROL_ORIGIN},
        )
        assert missing_csrf.status_code == 403

        wrong_reauth = await client.post(
            "/v1/control/session/reauthenticate",
            json={"schema_version": "1.0", "password": "wrong password value"},
            headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: csrf},
        )
        assert wrong_reauth.status_code == 401

        reauthenticated = await client.post(
            "/v1/control/session/reauthenticate",
            json={"schema_version": "1.0", "password": CONTROL_PASSWORD},
            headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: csrf},
        )
        assert reauthenticated.status_code == 200
        assert reauthenticated.json()["resource_version"] == 2
        rotated_csrf = reauthenticated.json()["csrf_token"]
        assert rotated_csrf != csrf

        old_csrf = await client.post(
            "/v1/control/session/logout",
            json={"schema_version": "1.0"},
            headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: csrf},
        )
        assert old_csrf.status_code == 403

        logout = await client.post(
            "/v1/control/session/logout",
            json={"schema_version": "1.0"},
            headers={"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: rotated_csrf},
        )
        assert logout.status_code == 200
        assert logout.json() == {"schema_version": "1.0", "revoked": True}
        assert "Max-Age=0" in logout.headers["set-cookie"]
        after_logout = await client.get("/v1/control/session")
        assert after_logout.status_code == 401

    async with app.state.database.sessions() as session:
        stored_session = await session.scalar(select(ControlPlaneSession))
        attempts = (await session.scalars(select(ControlPlaneLoginAttempt))).all()
        audits = (
            await session.scalars(
                select(ControlPlaneAuditEvent).order_by(ControlPlaneAuditEvent.created_at)
            )
        ).all()

    assert stored_session is not None
    assert stored_session.token_hash != cookie
    assert stored_session.csrf_hash not in {csrf, rotated_csrf}
    assert stored_session.revoked_at is not None
    assert [attempt.outcome for attempt in attempts] == ["rejected", "accepted"]
    assert [(audit.event, audit.outcome) for audit in audits] == [
        ("session_login", "rejected"),
        ("session_login", "accepted"),
        ("session_reauthenticate", "rejected"),
        ("session_reauthenticate", "accepted"),
        ("session_logout", "accepted"),
    ]
    serialized_evidence = json.dumps(
        [audit.evidence_json for audit in audits],
        sort_keys=True,
    )
    for secret in (CONTROL_PASSWORD, "wrong password value", cookie, csrf, rotated_csrf):
        assert secret not in serialized_evidence


async def test_login_rate_limit_and_unknown_operator_do_not_leak_identity(app) -> None:
    _enable_control(app, login_attempts=2)
    async with _control_client(app) as client:
        first = await client.post(
            "/v1/control/session/login",
            json=_login_payload(operator_id="unknown.user"),
            headers={"origin": CONTROL_ORIGIN},
        )
        second = await client.post(
            "/v1/control/session/login",
            json=_login_payload(password="wrong password value"),
            headers={"origin": CONTROL_ORIGIN},
        )
        limited = await client.post(
            "/v1/control/session/login",
            json=_login_payload(),
            headers={"origin": CONTROL_ORIGIN},
        )

    assert first.status_code == 401
    assert second.status_code == 401
    assert first.json() == second.json() == {"detail": "invalid control credentials"}
    assert limited.status_code == 429


async def test_operator_removal_and_database_expiry_invalidate_existing_session(app) -> None:
    _enable_control(app)
    async with _control_client(app) as client:
        login = await client.post(
            "/v1/control/session/login",
            json=_login_payload(),
            headers={"origin": CONTROL_ORIGIN},
        )
        assert login.status_code == 200

        active_settings = app.state.settings
        app.state.settings = replace(active_settings, control_operators={})
        assert (await client.get("/v1/control/session")).status_code == 503
        app.state.settings = active_settings

        async with app.state.database.sessions() as session:
            record = await session.scalar(select(ControlPlaneSession))
            assert record is not None
            original_issued_at = record.issued_at
            record.issued_at = original_issued_at - timedelta(hours=2)
            record.last_reauthenticated_at = original_issued_at - timedelta(hours=2)
            record.expires_at = original_issued_at - timedelta(hours=1)
            await session.commit()

        assert (await client.get("/v1/control/session")).status_code == 401


async def test_control_evidence_tables_reject_update_and_delete(app) -> None:
    _enable_control(app)
    async with _control_client(app) as client:
        login = await client.post(
            "/v1/control/session/login",
            json=_login_payload(),
            headers={"origin": CONTROL_ORIGIN},
        )
        assert login.status_code == 200

    async with app.state.database.sessions() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(
                update(ControlPlaneLoginAttempt).values(reason_code="tampered")
            )
            await session.commit()
        await session.rollback()

    async with app.state.database.sessions() as session:
        with pytest.raises(DBAPIError, match="append-only"):
            await session.execute(delete(ControlPlaneAuditEvent))
            await session.commit()
        await session.rollback()


async def test_concurrent_reauthentication_accepts_old_csrf_at_most_once(app) -> None:
    _enable_control(app)
    async with _control_client(app) as client:
        login = await client.post(
            "/v1/control/session/login",
            json=_login_payload(),
            headers={"origin": CONTROL_ORIGIN},
        )
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        request = {
            "json": {"schema_version": "1.0", "password": CONTROL_PASSWORD},
            "headers": {"origin": CONTROL_ORIGIN, CONTROL_CSRF_HEADER: csrf},
        }

        results = await asyncio.gather(
            client.post("/v1/control/session/reauthenticate", **request),
            client.post("/v1/control/session/reauthenticate", **request),
        )

    statuses = sorted(response.status_code for response in results)
    assert statuses[0] == 200
    assert statuses[1] in {403, 409}
    async with app.state.database.sessions() as session:
        record = await session.scalar(select(ControlPlaneSession))
        accepted = (
            await session.scalars(
                select(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.event == "session_reauthenticate",
                    ControlPlaneAuditEvent.outcome == "accepted",
                )
            )
        ).all()
    assert record is not None and record.resource_version == 2
    assert len(accepted) == 1
