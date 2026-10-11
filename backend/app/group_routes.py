"""Authenticated Facebook Group recommendation and selection API (manual posting only)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from app import auth
from app.group_recommendations import (
    apply_selection,
    eligible_groups,
    generate_recommendations,
    recommendations_view,
    record_decision,
)

router = APIRouter(prefix="/api", dependencies=[Depends(auth.require_session)])
test_router = APIRouter(prefix="/api/test", dependencies=[Depends(auth.require_session)])


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GroupDecisionInput(StrictInput):
    expected_revision: StrictInt = Field(ge=1)
    group_record_id: StrictStr = Field(min_length=36, max_length=36)
    decision: Literal["SELECT", "REJECT"]
    recommendation_id: StrictStr | None = Field(default=None, min_length=36, max_length=36)
    confirm_withdraw_approval: StrictBool = False


class ApplySelectionInput(StrictInput):
    expected_revision: StrictInt = Field(ge=1)


class RankerScenarioInput(StrictInput):
    mode: Literal["NORMAL", "FAIL"]


@router.get("/campaigns/{campaign_id}/group-recommendations")
def get_recommendations(campaign_id: str, request: Request):
    return recommendations_view(request.app.state.database.sessions, campaign_id)


@router.post("/campaigns/{campaign_id}/group-recommendations")
def refresh_recommendations(campaign_id: str, request: Request):
    return generate_recommendations(
        request.app.state.database.sessions, campaign_id, request.app.state.group_ranker
    )


@router.get("/campaigns/{campaign_id}/eligible-groups")
def browse_eligible(
    campaign_id: str,
    request: Request,
    q: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
):
    return eligible_groups(request.app.state.database.sessions, campaign_id, q, limit)


@router.post("/campaigns/{campaign_id}/group-decisions")
def decide(
    campaign_id: str,
    body: GroupDecisionInput,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    sessions = request.app.state.database.sessions
    record_decision(
        sessions,
        campaign_id,
        current.user_id,
        expected_revision=body.expected_revision,
        group_record_id=body.group_record_id,
        decision=body.decision,
        recommendation_id=body.recommendation_id,
        confirm_withdraw_approval=body.confirm_withdraw_approval,
    )
    return recommendations_view(sessions, campaign_id)


@router.post("/campaigns/{campaign_id}/group-selection")
def save_selection(
    campaign_id: str,
    body: ApplySelectionInput,
    request: Request,
    current: Annotated[auth.CurrentSession, Depends(auth.require_session)],
):
    return apply_selection(
        request.app.state.database.sessions, campaign_id, current.user_id, body.expected_revision
    )


@test_router.post("/group-ranker-scenario", status_code=204)
def configure_ranker_scenario(body: RankerScenarioInput, request: Request):
    scenarios = getattr(request.app.state, "group_ranker_scenarios", None)
    if scenarios is None:
        raise HTTPException(404, "NOT_FOUND")
    scenarios.mode = body.mode
