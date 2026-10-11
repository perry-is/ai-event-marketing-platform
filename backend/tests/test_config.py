from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def settings(**overrides):
    values = {
        "_env_file": None,
        "environment": "development",
        "app_origin": "http://localhost:5173",
        "database_url": "postgresql+psycopg://emp_local:local-only-password@127.0.0.1:5433/emp_local",
    }
    values.update(overrides)
    return Settings(**values)


def test_local_configuration_uses_loopback_and_has_no_auth_bypass():
    configured = settings()
    assert configured.cookie_secure is False
    assert configured.session_cookie == "emp_session"
    assert configured.oidc_configured is False
    assert configured.dev_auth_enabled is False
    assert configured.private_storage_dir.is_absolute()


@pytest.mark.parametrize(
    "field",
    [
        "eventbrite_execution_mode",
        "meetup_execution_mode",
        "linkedin_organization_execution_mode",
        "instagram_execution_mode",
    ],
)
def test_destination_modes_are_explicit_and_reject_unknown_values(field):
    assert getattr(settings(), field) == "SIMULATED"
    with pytest.raises(ValidationError):
        settings(**{field: "AUTOMATIC"})


def test_instagram_configuration_defaults_and_rejects_malformed_values():
    configured = settings()
    assert configured.instagram_execution_mode == "SIMULATED"
    assert configured.instagram_graph_api_version == "v26.0"
    assert configured.instagram_professional_account_id == ""
    assert configured.public_media_base_url == ""
    with pytest.raises(ValidationError):
        settings(instagram_graph_api_version="202510")
    with pytest.raises(ValidationError):
        settings(instagram_professional_account_id="account-name")
    with pytest.raises(ValidationError):
        settings(public_media_base_url="https://media.example.test/path")
    with pytest.raises(ValidationError):
        settings(public_media_base_url="http://media.example.test")
    with pytest.raises(ValidationError):
        settings(
            public_media_base_url="https://localhost",
            environment="production",
            app_origin="https://business.example.test",
            database_url="postgresql+psycopg://operator:prod-password@db/business",
            google_client_id="client-id",
            google_client_secret="test",
            allowed_emails=["owner@example.test"],
            extraction_mode="openai",
            campaign_generation_mode="openai",
            image_generation_mode="openai",
            group_recommendation_mode="openai",
            openai_campaign_model="gpt-6-luna",
            openai_api_key="test-provider-key",
            instagram_execution_mode="LIVE",
            media_delivery_signing_secret="x" * 40,
        )


def test_live_instagram_production_requires_https_media_and_signing_secret():
    base = {
        "environment": "production",
        "app_origin": "https://business.example.test",
        "database_url": "postgresql+psycopg://operator:prod-password@db/business",
        "google_client_id": "client-id",
        "google_client_secret": "test",
        "allowed_emails": ["owner@example.test"],
        "extraction_mode": "openai",
        "campaign_generation_mode": "openai",
        "image_generation_mode": "openai",
        "group_recommendation_mode": "openai",
        "openai_campaign_model": "gpt-6-luna",
        "openai_api_key": "test-provider-key",
        "instagram_execution_mode": "LIVE",
    }
    with pytest.raises(ValidationError):
        settings(**base)
    configured = settings(
        **base,
        public_media_base_url="https://media.example.test",
        media_delivery_signing_secret="x" * 40,
    )
    assert configured.instagram_execution_mode == "LIVE"


def test_production_configuration_requires_https_oidc_allowlist_and_nonlocal_database():
    configured = settings(
        environment="production",
        app_origin="https://business.example.test",
        database_url="postgresql+psycopg://operator:not-a-local-placeholder@db/business",
        google_client_id="client-id",
        google_client_secret="test",
        allowed_emails=["owner@example.test"],
        extraction_mode="openai",
        campaign_generation_mode="openai",
        image_generation_mode="openai",
        group_recommendation_mode="openai",
        openai_campaign_model="gpt-6-luna",
        openai_api_key="test-provider-key",
    )
    assert configured.cookie_secure is True
    assert configured.session_cookie == "__Host-emp_session"


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_origin": "http://business.example.test"},
        {"google_client_secret": ""},
        {"allowed_emails": ["*"]},
        {"dev_auth_enabled": True},
        {"database_url": "sqlite://"},
        {"database_url": "postgresql+psycopg://emp_local:local-only-password@db/emp_local"},
        {"extraction_mode": "fake"},
        {"extraction_mode": "openai", "openai_api_key": ""},
        {"campaign_generation_mode": "fake"},
        {"image_generation_mode": "fake"},
        {"image_generation_mode": "openai", "openai_api_key": ""},
        {"campaign_generation_mode": "openai", "openai_campaign_model": ""},
        {"campaign_generation_mode": "openai", "openai_api_key": ""},
        {"openai_campaign_model": "../not-a-model"},
        {"group_recommendation_mode": "fake"},
        {"group_recommendation_mode": "openai", "openai_api_key": ""},
        {"openai_group_recommendation_model": "../not-a-model"},
    ],
)
def test_unsafe_configuration_is_rejected(overrides):
    base = {
        "environment": "production",
        "app_origin": "https://business.example.test",
        "database_url": "postgresql+psycopg://operator:not-a-local-placeholder@db/business",
        "google_client_id": "client-id",
        "google_client_secret": "test",
        "allowed_emails": ["owner@example.test"],
        "extraction_mode": "openai",
        "campaign_generation_mode": "openai",
        "image_generation_mode": "openai",
        "group_recommendation_mode": "openai",
        "openai_campaign_model": "gpt-6-luna",
        "openai_api_key": "test-provider-key",
    }
    base.update(overrides)
    with pytest.raises(ValidationError):
        settings(**base)


def test_private_source_storage_rejects_relative_or_public_paths(tmp_path):
    with pytest.raises(ValidationError):
        settings(private_storage_dir=Path("relative/uploads"))
    public_path = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "uploads"
    with pytest.raises(ValidationError):
        settings(private_storage_dir=public_path)
    assert (
        settings(private_storage_dir=tmp_path / "private").private_storage_dir
        == (tmp_path / "private").resolve()
    )


def test_openai_mode_requires_server_key_and_production_rejects_fake_mode():
    with pytest.raises(ValidationError):
        settings(extraction_mode="openai")
    assert settings(extraction_mode="openai", openai_api_key="local-test-key").openai_api_key


def test_campaign_openai_mode_requires_server_key_and_model():
    with pytest.raises(ValidationError):
        settings(campaign_generation_mode="openai", openai_api_key="local-key")
    with pytest.raises(ValidationError):
        settings(
            campaign_generation_mode="openai",
            openai_api_key="local-key",
            openai_campaign_model="",
        )
    configured = settings(
        campaign_generation_mode="openai",
        openai_api_key="local-key",
        openai_campaign_model="gpt-6-luna-test",
    )
    assert configured.openai_campaign_model == "gpt-6-luna-test"
