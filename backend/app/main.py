import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.campaign_ai import OpenAICampaignService
from app.campaign_routes import router as campaign_router
from app.campaign_service import DeterministicCampaignService
from app.config import ROOT, Settings
from app.credential_vault import (
    CredentialKeyProvider,
    CredentialVault,
    FileCredentialKeyProvider,
    InMemoryCredentialKeyProvider,
)
from app.db import Database
from app.description_ai import DeterministicDescriptionSuggester, OpenAIDescriptionSuggester
from app.event_routes import router as event_router
from app.execution_routes import router as execution_router
from app.execution_routes import test_router as simulation_test_router
from app.execution_service import CampaignExecutionService
from app.extraction import DeterministicDemoExtractor, OpenAIExtractionService
from app.graphics import FakeImageProvider, OpenAIImageProvider
from app.group_ranker import DeterministicGroupRanker, GroupRankerScenario, OpenAIGroupRanker
from app.group_routes import router as group_router
from app.group_routes import test_router as group_test_router
from app.instagram import VaultInstagramCredentials, public_media_router
from app.linkedin import VaultLinkedInCredentials
from app.oauth_connections import (
    LiveOAuthGateway,
    OAuthConnections,
    connection_status_router,
    oauth_router,
)
from app.oauth_connections import (
    callback_router as oauth_callback_router,
)
from app.oidc import GoogleOIDC
from app.routes import LoginLimiter, private, public
from app.settings_routes import router as settings_router


def create_app(
    settings: Settings | None = None,
    oidc=None,
    *,
    openai_http_client=None,
    image_provider=None,
    eventbrite_http_client=None,
    meetup_http_client=None,
    linkedin_http_client=None,
    linkedin_credentials=None,
    instagram_http_client=None,
    instagram_credentials=None,
    oauth_gateway=None,
    credential_key_provider: CredentialKeyProvider | None = None,
) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # Callback query strings contain authorization codes. Disable raw access logging.
    logging.getLogger("uvicorn.access").disabled = True
    if settings is None:
        try:
            settings = Settings()
        except Exception:
            raise RuntimeError(
                "Invalid application configuration; check documented settings"
            ) from None
    if oidc is not None and settings.environment != "test":
        raise RuntimeError("Provider substitution is permitted only in test configuration")
    if openai_http_client is not None and settings.environment != "test":
        raise RuntimeError("OpenAI transport substitution is permitted only in test configuration")
    if image_provider is not None and settings.environment != "test":
        raise RuntimeError("Image provider substitution is permitted only in test configuration")
    if meetup_http_client is not None and settings.environment != "test":
        raise RuntimeError("Meetup transport substitution is permitted only in test configuration")
    if linkedin_http_client is not None and settings.environment != "test":
        raise RuntimeError(
            "LinkedIn transport substitution is permitted only in test configuration"
        )
    if linkedin_credentials is not None and settings.environment != "test":
        raise RuntimeError(
            "LinkedIn credential substitution is permitted only in test configuration"
        )
    if instagram_http_client is not None and settings.environment != "test":
        raise RuntimeError(
            "Instagram transport substitution is permitted only in test configuration"
        )
    if instagram_credentials is not None and settings.environment != "test":
        raise RuntimeError(
            "Instagram credential substitution is permitted only in test configuration"
        )
    if oauth_gateway is not None and settings.environment != "test":
        raise RuntimeError("OAuth gateway substitution is permitted only in test configuration")
    if credential_key_provider is not None and settings.environment != "test":
        raise RuntimeError("Credential key substitution is permitted only in test configuration")
    database = Database(settings)
    key_provider = credential_key_provider
    if key_provider is None:
        if settings.environment == "test":
            # Ephemeral per-process key; no reusable key material exists in source.
            key_provider = InMemoryCredentialKeyProvider({"test-key": os.urandom(32)}, "test-key")
        else:
            key_provider = FileCredentialKeyProvider(
                settings.credential_vault_keyring_path,
                settings.credential_vault_active_key_id,
                ROOT,
            )
    credential_vault = CredentialVault(key_provider)
    oauth_gateway = oauth_gateway or LiveOAuthGateway(settings)
    oauth_connections = OAuthConnections(database, credential_vault, settings, oauth_gateway)
    linkedin_vault_credentials = VaultLinkedInCredentials(oauth_connections, linkedin_credentials)
    instagram_vault_credentials = VaultInstagramCredentials(
        oauth_connections, instagram_credentials
    )

    extractor = (
        OpenAIExtractionService(
            api_key=settings.openai_api_key.get_secret_value(),
            model_id=settings.openai_extraction_model,
            http_client=openai_http_client,
        )
        if settings.extraction_mode == "openai"
        else DeterministicDemoExtractor()
    )
    campaign_service = (
        OpenAICampaignService(
            api_key=settings.openai_api_key.get_secret_value(),
            model_id=settings.openai_campaign_model,
            http_client=openai_http_client,
        )
        if settings.campaign_generation_mode == "openai"
        else DeterministicCampaignService()
    )
    description_suggester = (
        OpenAIDescriptionSuggester(
            api_key=settings.openai_api_key.get_secret_value(),
            model_id=settings.openai_campaign_model,
            http_client=openai_http_client,
        )
        if settings.campaign_generation_mode == "openai"
        else DeterministicDescriptionSuggester()
    )
    image_service = image_provider or (
        OpenAIImageProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model_id=settings.openai_image_model,
            http_client=openai_http_client,
        )
        if settings.image_generation_mode == "openai"
        else FakeImageProvider()
    )
    group_scenarios = (
        GroupRankerScenario(settings.environment) if settings.environment == "test" else None
    )
    group_ranker = (
        OpenAIGroupRanker(
            api_key=settings.openai_api_key.get_secret_value(),
            model_id=settings.openai_group_recommendation_model,
            http_client=openai_http_client,
        )
        if settings.group_recommendation_mode == "openai"
        else DeterministicGroupRanker(group_scenarios)
    )

    @asynccontextmanager
    async def lifespan(app):
        yield
        database.engine.dispose()
        close_oauth = getattr(oauth_gateway, "close", None)
        if close_oauth:
            close_oauth()
        close_extractor = getattr(extractor, "close", None)
        if close_extractor:
            close_extractor()
        close_campaign_service = getattr(campaign_service, "close", None)
        if close_campaign_service:
            close_campaign_service()
        close_description = getattr(description_suggester, "close", None)
        if close_description:
            close_description()
        close_image_service = getattr(image_service, "close", None)
        if close_image_service:
            close_image_service()
        close_group_ranker = getattr(group_ranker, "close", None)
        if close_group_ranker:
            close_group_ranker()
        for adapter in app.state.execution_service.adapters.adapters.values():
            close_adapter = getattr(adapter, "close", None)
            if close_adapter:
                close_adapter()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.database, app.state.extractor = settings, database, extractor
    app.state.credential_vault = credential_vault
    app.state.oauth_connections = oauth_connections
    app.state.campaign_service = campaign_service
    app.state.description_suggester = description_suggester
    app.state.image_provider = image_service
    app.state.group_ranker = group_ranker
    app.state.group_ranker_scenarios = group_scenarios
    app.state.execution_service = CampaignExecutionService(
        settings,
        database,
        eventbrite_http_client=eventbrite_http_client,
        meetup_http_client=meetup_http_client,
        linkedin_http_client=linkedin_http_client,
        linkedin_credentials=linkedin_vault_credentials,
        instagram_http_client=instagram_http_client,
        instagram_credentials=instagram_vault_credentials,
    )
    app.state.oidc = oidc or GoogleOIDC(settings)
    app.state.login_limiter = LoginLimiter()
    allowed_hosts = [urlsplit(settings.app_origin).hostname, "127.0.0.1"]
    if settings.public_media_base_url:
        media_host = urlsplit(settings.public_media_base_url).hostname
        if media_host and media_host not in allowed_hosts:
            allowed_hosts.append(media_host)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def private_response_policy(request: Request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            # Do not let server middleware re-raise/log raw credential-bearing errors.
            logging.getLogger("event_marketing").error("request_failed")
            response = JSONResponse({"error": {"code": "REQUEST_FAILED"}}, status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/") and response.status_code == 401:
            from app.auth import clear_cookie

            clear_cookie(response, settings, settings.session_cookie)
        return response

    @app.exception_handler(HTTPException)
    async def safe_http_error(request, error):
        return JSONResponse(
            {"error": {"code": error.detail}}, status_code=error.status_code, headers=error.headers
        )

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, error):
        return JSONResponse({"error": {"code": "INVALID_REQUEST"}}, status_code=422)

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(request, error):
        logging.getLogger("event_marketing").warning("database_unavailable")
        return JSONResponse({"error": {"code": "DEPENDENCY_UNAVAILABLE"}}, status_code=503)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        logging.getLogger("event_marketing").error("request_failed")
        return JSONResponse({"error": {"code": "REQUEST_FAILED"}}, status_code=500)

    app.include_router(public)
    app.include_router(private)
    app.include_router(event_router)
    app.include_router(campaign_router)
    app.include_router(execution_router)
    app.include_router(group_router)
    app.include_router(settings_router)
    app.include_router(oauth_router)
    app.include_router(connection_status_router)
    app.include_router(oauth_callback_router)
    app.include_router(public_media_router)
    if settings.environment == "test":
        app.include_router(simulation_test_router)
        app.include_router(group_test_router)
    for name in ("authlib", "httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("event_marketing").setLevel(logging.INFO)
    return app
