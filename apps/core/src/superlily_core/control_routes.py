"""默认禁用的最小控制面会话 HTTP 表面。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response
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
from .descriptor_mutations import (
    DescriptorLifecycleApplyIn,
    DescriptorLifecyclePreviewIn,
    apply_descriptor_lifecycle_mutation,
    create_descriptor_lifecycle_preview,
)
from .provider_mutations import (
    ProviderLifecycleApplyIn,
    ProviderLifecyclePreviewIn,
    apply_provider_lifecycle_mutation,
    create_provider_lifecycle_preview,
)
from .rollout_mutations import (
    RolloutPlanLifecycleApplyIn,
    RolloutPlanLifecyclePreviewIn,
    apply_rollout_plan_lifecycle_mutation,
    create_rollout_plan_lifecycle_preview,
)


router = APIRouter(prefix="/v1/control/session", tags=["control-session"])
descriptor_router = APIRouter(
    prefix="/v1/control/descriptors",
    tags=["control-descriptors"],
)
provider_router = APIRouter(
    prefix="/v1/control/providers",
    tags=["control-providers"],
)
rollout_router = APIRouter(
    prefix="/v1/control/rollout-plans",
    tags=["control-rollout-plans"],
)
Session = Annotated[AsyncSession, Depends(get_session)]
ControlIdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,255}$",
    ),
]


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


@descriptor_router.post(
    "/lifecycle/preview",
    dependencies=[Depends(verify_control_write_boundary)],
)
async def descriptor_lifecycle_preview(
    payload: DescriptorLifecyclePreviewIn,
    request: Request,
    response: Response,
    session: Session,
) -> dict:
    _no_store(response)
    return await create_descriptor_lifecycle_preview(
        session,
        request,
        payload,
        request.app.state.settings,
    )


@descriptor_router.post(
    "/lifecycle/apply",
    dependencies=[Depends(verify_control_write_boundary)],
)
async def descriptor_lifecycle_apply(
    payload: DescriptorLifecycleApplyIn,
    request: Request,
    response: Response,
    session: Session,
    idempotency_key: ControlIdempotencyKey,
) -> dict:
    _no_store(response)
    result, status_code = await apply_descriptor_lifecycle_mutation(
        session,
        request,
        payload,
        idempotency_key,
        request.app.state.settings,
    )
    response.status_code = status_code
    return result


@provider_router.post(
    "/lifecycle/preview",
    dependencies=[Depends(verify_control_write_boundary)],
)
async def provider_lifecycle_preview(
    payload: ProviderLifecyclePreviewIn,
    request: Request,
    response: Response,
    session: Session,
) -> dict:
    _no_store(response)
    return await create_provider_lifecycle_preview(
        session,
        request,
        payload,
        request.app.state.settings,
    )


@provider_router.post(
    "/lifecycle/apply",
    dependencies=[Depends(verify_control_write_boundary)],
)
async def provider_lifecycle_apply(
    payload: ProviderLifecycleApplyIn,
    request: Request,
    response: Response,
    session: Session,
    idempotency_key: ControlIdempotencyKey,
) -> dict:
    _no_store(response)
    result, status_code = await apply_provider_lifecycle_mutation(
        session,
        request,
        payload,
        idempotency_key,
        request.app.state.settings,
    )
    response.status_code = status_code
    return result


@rollout_router.post(
    "/lifecycle/preview",
    dependencies=[Depends(verify_control_write_boundary)],
)
async def rollout_plan_lifecycle_preview(
    payload: RolloutPlanLifecyclePreviewIn,
    request: Request,
    response: Response,
    session: Session,
) -> dict:
    _no_store(response)
    return await create_rollout_plan_lifecycle_preview(
        session,
        request,
        payload,
        request.app.state.settings,
    )


@rollout_router.post(
    "/lifecycle/apply",
    dependencies=[Depends(verify_control_write_boundary)],
)
async def rollout_plan_lifecycle_apply(
    payload: RolloutPlanLifecycleApplyIn,
    request: Request,
    response: Response,
    session: Session,
    idempotency_key: ControlIdempotencyKey,
) -> dict:
    _no_store(response)
    result, status_code = await apply_rollout_plan_lifecycle_mutation(
        session,
        request,
        payload,
        idempotency_key,
        request.app.state.settings,
    )
    response.status_code = status_code
    return result
