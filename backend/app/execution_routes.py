"""Authenticated approval and simulated destination execution commands."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from app import auth
from app.execution_service import (
    acknowledge_failure,
    acknowledge_uncertain,
    complete_manual,
    distribution_view,
    handoff_manual,
    skip_execution,
)

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])
test_router = APIRouter(prefix="/api/test", dependencies=[Depends(auth.require_session)])


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ApproveVersion(StrictInput):
    expected_revision: StrictInt = Field(ge=1)
    campaign_version_id: StrictStr = Field(min_length=36, max_length=36)
    version_hash: StrictStr = Field(min_length=64, max_length=64)
    expected_destination_plan_hash: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    replacement_acknowledgements: list[StrictStr] = Field(default_factory=list, max_length=40)
    uncertain_risk_acknowledgements: list[StrictStr] = Field(default_factory=list, max_length=40)


class UserNote(StrictInput):
    note: StrictStr | None = Field(default=None, max_length=500)


class SimulationScenario(StrictInput):
    outcomes: dict[
        StrictStr,
        list[Literal["SUCCESS", "FAILED_RETRYABLE", "FAILED_FINAL", "UNKNOWN"]],
    ] = Field(default_factory=dict)
    verifications: dict[
        StrictStr,
        list[Literal["FOUND_MATCHING", "CONFIRMED_ABSENT", "INCONCLUSIVE"]],
    ] = Field(default_factory=dict)


@router.get("/campaigns/{campaign_id}/distribution")
def get_distribution(campaign_id: str, request: Request):
    return distribution_view(request.app.state.database.sessions, campaign_id)


@router.get("/campaigns/{campaign_id}/approval-preview")
def get_approval_preview(campaign_id: str, request: Request):
    return request.app.state.execution_service.preview(
        request.app.state.database.sessions, campaign_id
    )


@router.post("/campaigns/{campaign_id}/approve")
def approve_campaign_version(
    campaign_id: str,
    body: ApproveVersion,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return request.app.state.execution_service.approve(
        request.app.state.database.sessions,
        campaign_id,
        current.user_id,
        expected_revision=body.expected_revision,
        version_id=body.campaign_version_id,
        version_hash=body.version_hash,
        expected_destination_plan_hash=body.expected_destination_plan_hash,
        replacement_acknowledgements=body.replacement_acknowledgements,
        uncertain_risk_acknowledgements=body.uncertain_risk_acknowledgements,
    )


@router.post("/approvals/{approval_id}/start")
def start_approved_execution(
    approval_id: str,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return request.app.state.execution_service.start(
        request.app.state.database.sessions, approval_id, current.user_id
    )


@router.post("/destination-executions/{execution_id}/retry")
def retry_destination(
    execution_id: str,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return request.app.state.execution_service.retry(
        request.app.state.database.sessions, execution_id, current.user_id
    )


@router.post("/destination-executions/{execution_id}/reconcile")
def reconcile_destination(
    execution_id: str,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return request.app.state.execution_service.reconcile(
        request.app.state.database.sessions, execution_id, current.user_id
    )


@router.post("/destination-executions/{execution_id}/manual-complete")
def complete_manual_action(
    execution_id: str,
    body: UserNote,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return complete_manual(
        request.app.state.database.sessions, execution_id, current.user_id, body.note
    )


@router.post("/destination-executions/{execution_id}/manual-handoff")
def handoff_manual_action(
    execution_id: str,
    body: UserNote,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return handoff_manual(
        request.app.state.database.sessions, execution_id, current.user_id, body.note
    )


@router.post("/destination-executions/{execution_id}/acknowledge-failure")
def acknowledge_destination_failure(
    execution_id: str,
    body: UserNote,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return acknowledge_failure(
        request.app.state.database.sessions, execution_id, current.user_id, body.note
    )


@router.post("/destination-executions/{execution_id}/acknowledge-uncertain")
def acknowledge_uncertain_destination(
    execution_id: str,
    body: UserNote,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return acknowledge_uncertain(
        request.app.state.database.sessions, execution_id, current.user_id, body.note
    )


@router.post("/destination-executions/{execution_id}/skip")
def skip_destination(
    execution_id: str,
    body: UserNote,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return skip_execution(
        request.app.state.database.sessions, execution_id, current.user_id, body.note
    )


@test_router.post("/simulation-scenario", status_code=204)
def configure_simulation_scenario(
    body: SimulationScenario,
    request: Request,
    _current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    if request.app.state.settings.environment != "test":
        raise HTTPException(404, "NOT_FOUND")
    scenarios = request.app.state.execution_service.scenarios
    if scenarios is None:
        raise HTTPException(404, "NOT_FOUND")
    try:
        scenarios.configure(body.outcomes, body.verifications)
    except ValueError:
        raise HTTPException(422, "SIMULATION_SCENARIO_INVALID") from None
    return None


@test_router.post("/reset-destination-modes", status_code=204)
def reset_test_destination_modes(
    request: Request,
    _current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    """Reset server modes between browser scenarios in the test-only API."""
    service = request.app.state.execution_service
    settings = request.app.state.settings
    if settings.environment != "test" or service.scenarios is None:
        raise HTTPException(404, "NOT_FOUND")
    settings.eventbrite_execution_mode = "SIMULATED"
    settings.meetup_execution_mode = "SIMULATED"
    settings.linkedin_organization_execution_mode = "SIMULATED"
    settings.instagram_execution_mode = "SIMULATED"
    service.live_connector_enabled = False
    service.rebuild()
    return None


def _configure_test_eventbrite_mode(
    request: Request, mode: str, live_connector: bool | None = None
) -> None:
    if request.app.state.settings.environment != "test":
        raise HTTPException(404, "NOT_FOUND")
    settings = request.app.state.settings
    settings.eventbrite_execution_mode = mode
    service = request.app.state.execution_service
    if live_connector is not None:
        if live_connector and service.eventbrite_http_client is None:
            raise HTTPException(404, "NOT_FOUND")
        service.live_connector_enabled = live_connector
    service.rebuild()


@test_router.post("/eventbrite-live-mode", status_code=204)
def enable_test_eventbrite_live_mode(request: Request):
    _configure_test_eventbrite_mode(request, "LIVE", live_connector=False)


@test_router.post("/eventbrite-live-connector", status_code=204)
def enable_test_eventbrite_live_connector(request: Request):
    """Real Eventbrite adapter over the test-only mocked HTTP transport (never the network)."""
    _configure_test_eventbrite_mode(request, "LIVE", live_connector=True)


@test_router.post("/eventbrite-simulated-mode", status_code=204)
def enable_test_eventbrite_simulated_mode(request: Request):
    _configure_test_eventbrite_mode(request, "SIMULATED")
