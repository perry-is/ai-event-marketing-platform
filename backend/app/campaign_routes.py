"""Authenticated campaign review and deterministic revision API."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from app import auth
from app.campaign_service import (
    SUPPORTED_SURFACES,
    Surface,
    create_or_get_campaign,
    get_campaign,
    get_campaign_for_event,
    get_campaign_version,
    regenerate_campaign,
    revise_campaign,
)
from app.graphics_service import campaign_graphic_bytes, create_graphic_successor

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GenerateCampaign(StrictInput):
    selected_surfaces: list[Surface] = Field(min_length=1, max_length=len(SUPPORTED_SURFACES))


class ReviseCampaign(StrictInput):
    expected_revision: StrictInt = Field(ge=1)
    revision_request: StrictStr = Field(min_length=3, max_length=500)


class RegenerateCampaign(StrictInput):
    expected_revision: StrictInt = Field(ge=1)


class CreateCampaignGraphic(StrictInput):
    expected_revision: StrictInt = Field(ge=1)
    instruction: StrictStr | None = Field(default=None, min_length=3, max_length=500)


@router.get("/events/{event_id}/campaign")
def campaign_for_event(event_id: str, request: Request):
    return get_campaign_for_event(request.app.state.database.sessions, event_id)


@router.post("/events/{event_id}/campaign")
def generate_campaign(
    event_id: str,
    body: GenerateCampaign,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return create_or_get_campaign(
        request.app.state.database.sessions,
        event_id,
        current.user_id,
        body.selected_surfaces,
        request.app.state.campaign_service,
    )


@router.get("/campaigns/{campaign_id}")
def campaign(campaign_id: str, request: Request):
    return get_campaign(request.app.state.database.sessions, campaign_id)


@router.get("/campaigns/{campaign_id}/versions/{version_number}")
def campaign_version(campaign_id: str, version_number: int, request: Request):
    return get_campaign_version(request.app.state.database.sessions, campaign_id, version_number)


@router.post("/campaigns/{campaign_id}/revisions")
def revise(
    campaign_id: str,
    body: ReviseCampaign,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return revise_campaign(
        request.app.state.database.sessions,
        campaign_id,
        current.user_id,
        body.expected_revision,
        body.revision_request,
        request.app.state.campaign_service,
    )


@router.post("/campaigns/{campaign_id}/regenerate")
def regenerate(
    campaign_id: str,
    body: RegenerateCampaign,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return regenerate_campaign(
        request.app.state.database.sessions,
        campaign_id,
        current.user_id,
        body.expected_revision,
        request.app.state.campaign_service,
    )


@router.post("/campaigns/{campaign_id}/graphics")
def create_graphic(
    campaign_id: str,
    body: CreateCampaignGraphic,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return create_graphic_successor(
        request.app.state.database.sessions,
        request.app.state.settings,
        campaign_id,
        current.user_id,
        body.expected_revision,
        body.instruction,
        request.app.state.image_provider,
    )


@router.get(
    "/campaign-versions/{version_id}/graphic",
    response_class=Response,
    responses={200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}},
)
def campaign_graphic(version_id: str, request: Request):
    data, mime_type = campaign_graphic_bytes(
        request.app.state.database.sessions, request.app.state.settings, version_id
    )
    return Response(data, media_type=mime_type, headers={"Content-Disposition": "inline"})
