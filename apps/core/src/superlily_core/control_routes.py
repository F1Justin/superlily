"""默认禁用的最小控制面会话 HTTP 表面。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from .control_plane import (
    ControlLoginIn,
    ControlLogoutIn,
    ControlReauthenticateIn,
    current_control_session,
    login_control_session,
    logout_control_session,
    reauthenticate_control_session,
    verify_control_write_boundary,
)
from .dependencies import get_session


router = APIRouter(prefix="/v1/control/session", tags=["control-session"])
Session = Annotated[AsyncSession, Depends(get_session)]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"


@router.post("/login", dependencies=[Depends(verify_control_write_boundary)])
async def login(
    payload: ControlLoginIn,
    request: Request,
    response: Response,
    session: Session,
) -> dict:
    return await login_control_session(
        session,
        request,
        response,
        payload,
        request.app.state.settings,
    )


@router.get("")
async def current(
    request: Request,
    response: Response,
    session: Session,
) -> dict:
    _no_store(response)
    return await current_control_session(session, request, request.app.state.settings)


@router.post("/reauthenticate", dependencies=[Depends(verify_control_write_boundary)])
async def reauthenticate(
    payload: ControlReauthenticateIn,
    request: Request,
    response: Response,
    session: Session,
) -> dict:
    _no_store(response)
    return await reauthenticate_control_session(
        session,
        request,
        payload,
        request.app.state.settings,
    )


@router.post("/logout", dependencies=[Depends(verify_control_write_boundary)])
async def logout(
    payload: ControlLogoutIn,
    request: Request,
    response: Response,
    session: Session,
) -> dict:
    del payload
    return await logout_control_session(
        session,
        request,
        response,
        request.app.state.settings,
    )
