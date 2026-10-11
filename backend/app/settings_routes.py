"""Authenticated, read-only connector status for Settings. Never returns credentials or IDs."""

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app import auth
from app.eventbrite import live_readiness
from app.meetup import live_readiness as meetup_live_readiness

router = APIRouter(prefix="/api/settings", dependencies=[Depends(auth.require_session)])
CurrentSettingsSession = Annotated[auth.CurrentSession, Depends(auth.require_session)]

REASON_TEXT = {
    "EVENTBRITE_TOKEN_MISSING": "Access token not configured",
    "EVENTBRITE_ORGANIZATION_MISSING": "Organization not configured",
    "EVENTBRITE_ORGANIZER_MISSING": "Organizer not configured",
    "EVENTBRITE_CURRENCY_MISSING": "Account currency not configured",
    "EVENTBRITE_TOKEN_REJECTED": "Eventbrite rejected the access token",
    "EVENTBRITE_PERMISSION_DENIED": "The token can't access this account",
    "EVENTBRITE_ORGANIZATION_UNAVAILABLE": "Configured organization isn't available to this token",
    "EVENTBRITE_ORGANIZER_UNAVAILABLE": "Configured organizer isn't part of this organization",
    "EVENTBRITE_RATE_LIMITED": "Eventbrite asked us to slow down; try again shortly",
    "EVENTBRITE_UNREACHABLE": "Eventbrite couldn't be reached",
    "MEETUP_CLIENT_KEY_MISSING": "Meetup client key not configured",
    "MEETUP_MEMBER_ID_MISSING": "Authorized Meetup account not configured",
    "MEETUP_SIGNING_KEY_ID_MISSING": "Meetup signing key ID not configured",
    "MEETUP_PRIVATE_KEY_MISSING": "Meetup private key not configured",
    "MEETUP_SIGNING_KEY_UNREADABLE": "Meetup signing key is not readable",
    "MEETUP_NETWORK_MISSING": "Meetup Pro Network not configured",
    "MEETUP_GROUP_MISSING": "Meetup Group not configured",
    "MEETUP_TOKEN_REJECTED": "Meetup rejected the connection",
    "MEETUP_PERMISSION_DENIED": "The Meetup account cannot access this Group",
    "MEETUP_ACCOUNT_MISMATCH": "Connected as an unexpected Meetup account",
    "MEETUP_NETWORK_UNAVAILABLE": "Configured Meetup Pro Network is unavailable",
    "MEETUP_GROUP_UNAVAILABLE": "Configured Meetup Group is unavailable",
    "MEETUP_GROUP_NETWORK_MISMATCH": "The configured Group is not in the selected Pro Network",
    "MEETUP_RATE_LIMITED": "Meetup asked us to slow down; try again later",
    "MEETUP_UNREACHABLE": "Meetup couldn't be reached",
    "MEETUP_CONFIGURATION_INVALID": "Meetup signing configuration needs attention",
    "LINKEDIN_CLIENT_ID_MISSING": "LinkedIn app client ID not configured",
    "LINKEDIN_CLIENT_SECRET_MISSING": "LinkedIn app client secret not configured",
    "LINKEDIN_REDIRECT_URI_MISSING": "LinkedIn sign-in callback not configured",
    "LINKEDIN_ORGANIZATION_MISSING": "LinkedIn Page not configured",
    "LINKEDIN_STANDARD_ACCESS_REQUIRED": (
        "Standard Community Management access is required for live publishing"
    ),
    "LINKEDIN_ACCESS_TIER_UNVERIFIED": (
        "LinkedIn Community Management access must be verified in the Developer Portal"
    ),
    "LINKEDIN_ACCOUNT_NOT_CONNECTED": "A LinkedIn member has not connected this application",
    "LINKEDIN_CREDENTIAL_VAULT_UNAVAILABLE": "Secure LinkedIn credential storage is unavailable",
    "LINKEDIN_REAUTH_REQUIRED": "Reconnect LinkedIn to continue live publishing",
    "LINKEDIN_REQUIRED_SCOPE_MISSING": "LinkedIn is missing a required permission",
    "LINKEDIN_ORGANIZATION_ROLE_MISSING": (
        "The connected member cannot publish to this LinkedIn Page"
    ),
    "LINKEDIN_ORGANIZATION_CHANGED": (
        "The approved LinkedIn Page no longer matches the configured Page"
    ),
    "LINKEDIN_TOKEN_REJECTED": "LinkedIn rejected the connection",
    "LINKEDIN_PERMISSION_DENIED": "The connected account lacks a required LinkedIn permission",
    "LINKEDIN_RATE_LIMITED": "LinkedIn asked us to slow down; try again later",
    "LINKEDIN_UNREACHABLE": "LinkedIn could not be reached",
    "LINKEDIN_UNAVAILABLE": "LinkedIn is temporarily unavailable",
    "LINKEDIN_READINESS_UNVERIFIED": "LinkedIn readiness could not be verified",
    "LINKEDIN_CONFIGURATION_INVALID": "LinkedIn setup needs attention",
    "INSTAGRAM_ACCOUNT_ID_MISSING": "Instagram Professional account is not configured",
    "INSTAGRAM_ACCOUNT_NOT_CONNECTED": "Instagram account has not connected this app",
    "INSTAGRAM_CREDENTIAL_VAULT_UNAVAILABLE": "Secure Instagram credential storage is unavailable",
    "INSTAGRAM_REAUTH_REQUIRED": "Reconnect Instagram to continue live publishing",
    "INSTAGRAM_APP_NOT_CONFIGURED": "Instagram app credentials are not configured",
    "INSTAGRAM_REDIRECT_URI_MISSING": "Instagram sign-in callback is not configured",
    "INSTAGRAM_REQUIRED_SCOPE_MISSING": "Instagram publishing permission is missing",
    "INSTAGRAM_ACCOUNT_MISMATCH": (
        "Connected Instagram account does not match the configured account"
    ),
    "INSTAGRAM_ACCOUNT_NOT_ELIGIBLE": "A Business or Creator Instagram account is required",
    "INSTAGRAM_MEDIA_DELIVERY_NOT_CONFIGURED": "Secure media delivery is not configured",
    "INSTAGRAM_MEDIA_DELIVERY_REQUIRES_HTTPS": "Secure media delivery needs a public HTTPS address",
    "INSTAGRAM_MEDIA_SIGNING_SECRET_MISSING": "Secure media delivery signing is not configured",
    "INSTAGRAM_LIMIT_REACHED": "Instagram publishing limit has been reached",
    "INSTAGRAM_TOKEN_REJECTED": "Instagram rejected the connection",
    "INSTAGRAM_PERMISSION_DENIED": "The connected Instagram account lacks permission",
    "INSTAGRAM_RATE_LIMITED": "Instagram asked us to slow down; try again later",
    "INSTAGRAM_UNREACHABLE": "Instagram could not be reached",
    "INSTAGRAM_UNAVAILABLE": "Instagram is temporarily unavailable",
    "INSTAGRAM_READINESS_UNVERIFIED": "Instagram readiness could not be verified",
    "INSTAGRAM_CONFIGURATION_INVALID": "Instagram setup needs attention",
}


@router.get("/eventbrite")
def eventbrite_status(request: Request):
    settings = request.app.state.settings
    service = request.app.state.execution_service
    client = service.eventbrite_client()
    try:
        result = live_readiness(settings, client)
    finally:
        if client is not None:
            client.close()
    return {
        "mode": result["mode"],
        "status": result["status"],
        "reasons": [REASON_TEXT.get(code, "Needs attention") for code in result["reasons"]],
        "organization_name": result["organization_name"],
        "organizer_name": result["organizer_name"],
        "checked_live": result["checked_live"],
    }


@router.get("/meetup")
def meetup_status(request: Request):
    settings = request.app.state.settings
    client = request.app.state.execution_service.meetup_client()
    try:
        result = meetup_live_readiness(settings, client)
    finally:
        if client is not None:
            client.close()
    return {
        "mode": result["mode"],
        "status": result["status"],
        "reasons": [REASON_TEXT.get(code, "Needs attention") for code in result["reasons"]],
        "member_name": result["member_name"],
        "network_name": result["network_name"],
        "group_name": result["group_name"],
        "checked_live": result["checked_live"],
        "photo_capability": result["photo_capability"],
        "photo_note": "Live image upload awaiting verification",
    }


@router.get("/linkedin-organization")
def linkedin_organization_status(request: Request, current: CurrentSettingsSession):
    if request.app.state.settings.environment != "test":
        settings = request.app.state.settings
        connection = request.app.state.oauth_connections.metadata("linkedin", current.user_id)
        reasons = []
        if (
            not settings.linkedin_client_id
            or not settings.linkedin_client_secret.get_secret_value()
        ):
            reasons.append("LINKEDIN_CLIENT_ID_MISSING")
        if not settings.linkedin_redirect_uri:
            reasons.append("LINKEDIN_REDIRECT_URI_MISSING")
        if connection["status"] == "NOT_CONNECTED":
            reasons.append("LINKEDIN_ACCOUNT_NOT_CONNECTED")
        elif connection["status"] == "REAUTH_REQUIRED":
            reasons.append("LINKEDIN_REAUTH_REQUIRED")
        if not connection["vault_ready"]:
            reasons.append("LINKEDIN_CREDENTIAL_VAULT_UNAVAILABLE")
        if connection["missing_scopes"]:
            reasons.append("LINKEDIN_REQUIRED_SCOPE_MISSING")
        if settings.linkedin_community_access_tier == "UNVERIFIED":
            reasons.append("LINKEDIN_ACCESS_TIER_UNVERIFIED")
        elif settings.linkedin_community_access_tier != "STANDARD":
            reasons.append("LINKEDIN_STANDARD_ACCESS_REQUIRED")
        return {
            "mode": settings.linkedin_organization_execution_mode,
            "status": "SIMULATED"
            if settings.linkedin_organization_execution_mode == "SIMULATED"
            else (
                "CONNECTED"
                if connection["status"] == "CONNECTED" and not reasons
                else "NEEDS_SETUP"
            ),
            "reasons": [REASON_TEXT.get(code, "Needs attention") for code in reasons],
            "organization_name": settings.linkedin_organization_name or None,
            "connected_member": connection["account_name"],
            "community_access_tier": settings.linkedin_community_access_tier,
            "checked_live": False,
            "connector_implemented": True,
        }
    result = request.app.state.execution_service.linkedin_status()
    return {
        "mode": result["mode"],
        "status": result["status"],
        "reasons": [REASON_TEXT.get(code, "Needs attention") for code in result["reasons"]],
        "organization_name": result["organization_name"],
        "connected_member": result["connected_member"],
        "community_access_tier": result["community_access_tier"],
        "checked_live": result["checked_live"],
        "connector_implemented": result["connector_implemented"],
    }


@router.get("/instagram")
def instagram_status(request: Request, current: CurrentSettingsSession):
    if request.app.state.settings.environment != "test":
        settings = request.app.state.settings
        connection = request.app.state.oauth_connections.metadata("instagram", current.user_id)
        reasons = []
        if (
            not settings.instagram_client_id
            or not settings.instagram_client_secret.get_secret_value()
        ):
            reasons.append("INSTAGRAM_APP_NOT_CONFIGURED")
        if not settings.instagram_redirect_uri:
            reasons.append("INSTAGRAM_REDIRECT_URI_MISSING")
        if connection["status"] == "NOT_CONNECTED":
            reasons.append("INSTAGRAM_ACCOUNT_NOT_CONNECTED")
        elif connection["status"] == "REAUTH_REQUIRED":
            reasons.append("INSTAGRAM_REAUTH_REQUIRED")
        if not connection["vault_ready"]:
            reasons.append("INSTAGRAM_CREDENTIAL_VAULT_UNAVAILABLE")
        if connection["missing_scopes"]:
            reasons.append("INSTAGRAM_REQUIRED_SCOPE_MISSING")
        account_eligible = connection["account_type"] in {"Business", "Media_Creator"}
        media_ready = bool(
            settings.public_media_base_url
            and settings.media_delivery_signing_secret.get_secret_value()
        )
        if not media_ready:
            reasons.append("INSTAGRAM_MEDIA_DELIVERY_NOT_CONFIGURED")
        return {
            "mode": settings.instagram_execution_mode,
            "status": "SIMULATED"
            if settings.instagram_execution_mode == "SIMULATED"
            else (
                "CONNECTED"
                if connection["status"] == "CONNECTED" and not reasons
                else "NEEDS_SETUP"
            ),
            "reasons": [REASON_TEXT.get(code, "Needs attention") for code in reasons],
            "username": connection["account_name"],
            "checked_live": False,
            "connector_implemented": True,
            "account_eligible": account_eligible,
            "media_delivery_ready": media_ready,
        }
    result = request.app.state.execution_service.instagram_status()
    return {
        "mode": result["mode"],
        "status": result["status"],
        "reasons": [REASON_TEXT.get(code, "Needs attention") for code in result["reasons"]],
        "username": result["username"],
        "checked_live": result["checked_live"],
        "connector_implemented": result["connector_implemented"],
        "account_eligible": result["account_eligible"],
        "media_delivery_ready": result["media_delivery_ready"],
    }
