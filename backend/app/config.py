import ipaddress
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]


def is_external_https_url(value: str) -> bool:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        if not ipaddress.ip_address(host).is_global:
            return False
    except ValueError:
        pass
    return True


# Maintained from LinkedIn's official Marketing API migration table. Update this
# allowlist when the next supported monthly version is released and older versions
# sunset; the application must fail closed rather than send an obsolete header.
LINKEDIN_SUPPORTED_API_VERSIONS = frozenset(
    {
        "202511",
        "202601",
        "202602",
        "202603",
        "202604",
        "202605",
        "202606",
        "202607",
        "202608",
        "202609",
    }
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EMP_",
        env_file=ROOT / ".env",
        extra="ignore",
        populate_by_name=True,
    )
    environment: Literal["production", "development", "test"] = "production"
    app_origin: str = "http://localhost:5173"
    database_url: SecretStr
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    allowed_emails: list[str] = []
    extraction_mode: Literal["fake", "openai"] = "fake"
    campaign_generation_mode: Literal["fake", "openai"] = "fake"
    image_generation_mode: Literal["fake", "openai"] = "fake"
    group_recommendation_mode: Literal["fake", "openai"] = "fake"
    business_default_timezone: str = Field(
        default="America/Denver",
        validation_alias=AliasChoices("DEFAULT_TIMEZONE", "EMP_DEFAULT_TIMEZONE"),
    )
    business_default_venue_name: str = Field(
        default="Example Conference Center",
        validation_alias=AliasChoices("DEFAULT_VENUE_NAME", "EMP_DEFAULT_VENUE_NAME"),
    )
    business_default_venue_street: str = Field(
        default="100 Main St",
        validation_alias=AliasChoices("DEFAULT_VENUE_STREET", "EMP_DEFAULT_VENUE_STREET"),
    )
    business_default_venue_city: str = Field(
        default="Springfield",
        validation_alias=AliasChoices("DEFAULT_VENUE_CITY", "EMP_DEFAULT_VENUE_CITY"),
    )
    business_default_venue_region: str = Field(
        default="CO",
        validation_alias=AliasChoices("DEFAULT_VENUE_REGION", "EMP_DEFAULT_VENUE_REGION"),
    )
    business_default_venue_postal_code: str = Field(
        default="80202",
        validation_alias=AliasChoices("DEFAULT_VENUE_POSTAL_CODE", "EMP_DEFAULT_VENUE_POSTAL_CODE"),
    )
    eventbrite_execution_mode: Literal["SIMULATED", "LIVE"] = "SIMULATED"
    # Server-only Eventbrite private token (V1). Never persisted or returned by any API.
    eventbrite_private_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("EVENTBRITE_PRIVATE_TOKEN", "EMP_EVENTBRITE_PRIVATE_TOKEN"),
    )
    eventbrite_organization_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "EVENTBRITE_ORGANIZATION_ID", "EMP_EVENTBRITE_ORGANIZATION_ID"
        ),
    )
    eventbrite_organizer_id: str = Field(
        default="",
        validation_alias=AliasChoices("EVENTBRITE_ORGANIZER_ID", "EMP_EVENTBRITE_ORGANIZER_ID"),
    )
    eventbrite_currency: str = Field(
        default="",
        validation_alias=AliasChoices("EVENTBRITE_CURRENCY", "EMP_EVENTBRITE_CURRENCY"),
    )
    meetup_execution_mode: Literal["SIMULATED", "LIVE"] = "SIMULATED"
    meetup_client_key: str = Field(
        default="",
        validation_alias=AliasChoices("MEETUP_CLIENT_KEY", "EMP_MEETUP_CLIENT_KEY"),
    )
    meetup_authorized_member_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "MEETUP_AUTHORIZED_MEMBER_ID", "EMP_MEETUP_AUTHORIZED_MEMBER_ID"
        ),
    )
    meetup_signing_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("MEETUP_SIGNING_KEY_ID", "EMP_MEETUP_SIGNING_KEY_ID"),
    )
    meetup_private_key_path: str = Field(
        default="",
        validation_alias=AliasChoices("MEETUP_PRIVATE_KEY_PATH", "EMP_MEETUP_PRIVATE_KEY_PATH"),
    )
    meetup_network_urlname: str = Field(
        default="",
        validation_alias=AliasChoices("MEETUP_NETWORK_URLNAME", "EMP_MEETUP_NETWORK_URLNAME"),
    )
    meetup_group_urlname: str = Field(
        default="",
        validation_alias=AliasChoices("MEETUP_GROUP_URLNAME", "EMP_MEETUP_GROUP_URLNAME"),
    )
    linkedin_organization_execution_mode: Literal["SIMULATED", "LIVE"] = "SIMULATED"
    linkedin_api_version: str = Field(
        default="202609",
        validation_alias=AliasChoices("LINKEDIN_API_VERSION", "EMP_LINKEDIN_API_VERSION"),
    )
    linkedin_client_id: str = Field(
        default="",
        validation_alias=AliasChoices("LINKEDIN_CLIENT_ID", "EMP_LINKEDIN_CLIENT_ID"),
    )
    linkedin_client_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("LINKEDIN_CLIENT_SECRET", "EMP_LINKEDIN_CLIENT_SECRET"),
    )
    linkedin_redirect_uri: str = Field(
        default="",
        validation_alias=AliasChoices("LINKEDIN_REDIRECT_URI", "EMP_LINKEDIN_REDIRECT_URI"),
    )
    linkedin_organization_id: str = Field(
        default="",
        validation_alias=AliasChoices("LINKEDIN_ORGANIZATION_ID", "EMP_LINKEDIN_ORGANIZATION_ID"),
    )
    linkedin_organization_name: str = Field(
        default="",
        validation_alias=AliasChoices(
            "LINKEDIN_ORGANIZATION_NAME", "EMP_LINKEDIN_ORGANIZATION_NAME"
        ),
    )
    linkedin_community_access_tier: Literal["UNVERIFIED", "DEVELOPMENT", "STANDARD"] = Field(
        default="UNVERIFIED",
        validation_alias=AliasChoices(
            "LINKEDIN_COMMUNITY_ACCESS_TIER", "EMP_LINKEDIN_COMMUNITY_ACCESS_TIER"
        ),
    )
    instagram_execution_mode: Literal["SIMULATED", "LIVE"] = "SIMULATED"
    instagram_graph_api_version: str = Field(
        default="v26.0",
        validation_alias=AliasChoices(
            "INSTAGRAM_GRAPH_API_VERSION", "EMP_INSTAGRAM_GRAPH_API_VERSION"
        ),
    )
    instagram_professional_account_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "INSTAGRAM_PROFESSIONAL_ACCOUNT_ID", "EMP_INSTAGRAM_PROFESSIONAL_ACCOUNT_ID"
        ),
    )
    instagram_username: str = Field(
        default="",
        validation_alias=AliasChoices("INSTAGRAM_USERNAME", "EMP_INSTAGRAM_USERNAME"),
    )
    instagram_client_id: str = Field(
        default="", validation_alias=AliasChoices("INSTAGRAM_CLIENT_ID", "EMP_INSTAGRAM_CLIENT_ID")
    )
    instagram_client_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("INSTAGRAM_CLIENT_SECRET", "EMP_INSTAGRAM_CLIENT_SECRET"),
    )
    instagram_redirect_uri: str = Field(
        default="",
        validation_alias=AliasChoices("INSTAGRAM_REDIRECT_URI", "EMP_INSTAGRAM_REDIRECT_URI"),
    )
    # Instagram Login app credentials stay in deployment configuration; delegated account
    # tokens are encrypted by the credential vault.
    public_media_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("PUBLIC_MEDIA_BASE_URL", "EMP_PUBLIC_MEDIA_BASE_URL"),
    )
    media_delivery_signing_secret: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "MEDIA_DELIVERY_SIGNING_SECRET", "EMP_MEDIA_DELIVERY_SIGNING_SECRET"
        ),
    )
    openai_extraction_model: str = Field(
        default="gpt-6-luna",
        validation_alias=AliasChoices("OPENAI_EXTRACTION_MODEL", "EMP_OPENAI_EXTRACTION_MODEL"),
    )
    openai_campaign_model: str = Field(
        default="",
        validation_alias=AliasChoices("OPENAI_CAMPAIGN_MODEL", "EMP_OPENAI_CAMPAIGN_MODEL"),
    )
    openai_image_model: str = Field(
        default="gpt-image-2.5-flare",
        validation_alias=AliasChoices("OPENAI_IMAGE_MODEL", "EMP_OPENAI_IMAGE_MODEL"),
    )
    openai_group_recommendation_model: str = Field(
        default="gpt-6-luna",
        validation_alias=AliasChoices(
            "OPENAI_GROUP_RECOMMENDATION_MODEL", "EMP_OPENAI_GROUP_RECOMMENDATION_MODEL"
        ),
    )
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("OPENAI_API_KEY", "EMP_OPENAI_API_KEY"),
    )
    # No runtime bypass exists; explicitly reject attempts to enable one.
    dev_auth_enabled: bool = False
    credential_vault_active_key_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "CREDENTIAL_VAULT_ACTIVE_KEY_ID", "EMP_CREDENTIAL_VAULT_ACTIVE_KEY_ID"
        ),
    )
    credential_vault_keyring_path: Path | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "CREDENTIAL_VAULT_KEYRING_PATH", "EMP_CREDENTIAL_VAULT_KEYRING_PATH"
        ),
    )
    private_storage_dir: Path = ROOT / "var" / "private"

    @field_validator("openai_extraction_model")
    @classmethod
    def valid_extraction_model(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 100:
            raise ValueError("OPENAI_EXTRACTION_MODEL must be a short non-empty model ID")
        return value

    @field_validator("openai_campaign_model")
    @classmethod
    def valid_campaign_model(cls, value: str) -> str:
        value = value.strip()
        if value and (len(value) > 100 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value)):
            raise ValueError("OPENAI_CAMPAIGN_MODEL must be a valid model identifier")
        return value

    @field_validator("openai_image_model")
    @classmethod
    def valid_image_model(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 100 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value):
            raise ValueError("OPENAI_IMAGE_MODEL must be a valid model identifier")
        return value

    @field_validator("openai_group_recommendation_model")
    @classmethod
    def valid_group_model(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 100 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value):
            raise ValueError("OPENAI_GROUP_RECOMMENDATION_MODEL must be a valid model identifier")
        return value

    @field_validator("eventbrite_organization_id", "eventbrite_organizer_id")
    @classmethod
    def valid_eventbrite_identifier(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[0-9]{1,30}", value):
            raise ValueError("Eventbrite identifiers must be numeric")
        return value

    @field_validator("eventbrite_currency")
    @classmethod
    def valid_eventbrite_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if value and not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("EVENTBRITE_CURRENCY must be a three-letter currency code")
        return value

    @field_validator("meetup_client_key", "meetup_signing_key_id", "meetup_authorized_member_id")
    @classmethod
    def valid_meetup_identifiers(cls, value: str, info) -> str:
        value = value.strip()
        if not value:
            return value
        pattern = (
            r"[0-9]{1,30}"
            if info.field_name == "meetup_authorized_member_id"
            else r"[A-Za-z0-9._:-]{1,160}"
        )
        if not re.fullmatch(pattern, value):
            raise ValueError(f"{info.field_name} has an invalid format")
        return value

    @field_validator("meetup_group_urlname", "meetup_network_urlname")
    @classmethod
    def valid_meetup_urlnames(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
            raise ValueError("Meetup URL names must be simple path components")
        return value

    @field_validator("linkedin_api_version")
    @classmethod
    def valid_linkedin_api_version(cls, value: str) -> str:
        value = value.strip()
        if value not in LINKEDIN_SUPPORTED_API_VERSIONS:
            raise ValueError("LINKEDIN_API_VERSION must be a currently supported YYYYMM version")
        return value

    @field_validator("linkedin_client_id", "linkedin_organization_name")
    @classmethod
    def bounded_linkedin_text(cls, value: str, info) -> str:
        value = value.strip()
        limit = 240 if info.field_name == "linkedin_organization_name" else 200
        if len(value) > limit:
            raise ValueError(f"{info.field_name} is too long")
        return value

    @field_validator("linkedin_organization_id")
    @classmethod
    def valid_linkedin_organization_id(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[0-9]{1,30}", value):
            raise ValueError("LINKEDIN_ORGANIZATION_ID must be numeric")
        return value

    @field_validator("instagram_graph_api_version")
    @classmethod
    def valid_instagram_api_version(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"v[0-9]{1,2}\.[0-9]", value):
            raise ValueError("INSTAGRAM_GRAPH_API_VERSION must use the vNN.N format")
        return value

    @field_validator("instagram_professional_account_id")
    @classmethod
    def valid_instagram_account_id(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[0-9]{1,32}", value):
            raise ValueError("INSTAGRAM_PROFESSIONAL_ACCOUNT_ID must be numeric")
        return value

    @field_validator("instagram_username")
    @classmethod
    def valid_instagram_username(cls, value: str) -> str:
        value = value.strip()
        if value and (len(value) > 64 or not re.fullmatch(r"[A-Za-z0-9._]+", value)):
            raise ValueError("INSTAGRAM_USERNAME has an invalid format")
        return value

    @field_validator("credential_vault_active_key_id")
    @classmethod
    def valid_vault_key_id(cls, value: str) -> str:
        value = value.strip()
        if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value):
            raise ValueError("CREDENTIAL_VAULT_ACTIVE_KEY_ID has an invalid format")
        return value

    @field_validator("credential_vault_keyring_path")
    @classmethod
    def valid_vault_keyring_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        expanded = value.expanduser()
        if not expanded.is_absolute():
            raise ValueError("CREDENTIAL_VAULT_KEYRING_PATH must be an absolute path")
        resolved = expanded.resolve()
        try:
            resolved.relative_to(ROOT.resolve())
        except ValueError:
            return resolved
        raise ValueError("Credential vault keyring must be stored outside the repository")

    @field_validator("instagram_redirect_uri")
    @classmethod
    def valid_instagram_redirect_uri(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})
        ):
            raise ValueError("INSTAGRAM_REDIRECT_URI must be HTTPS or loopback HTTP")
        return value

    @field_validator("public_media_base_url")
    @classmethod
    def valid_public_media_base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (
                parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        ):
            raise ValueError("PUBLIC_MEDIA_BASE_URL must be public HTTPS or loopback HTTP")
        return value

    @field_validator("linkedin_redirect_uri")
    @classmethod
    def valid_linkedin_redirect_uri(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"})
        ):
            raise ValueError("LINKEDIN_REDIRECT_URI must be an HTTPS callback or loopback HTTP URI")
        return value

    @field_validator("meetup_private_key_path")
    @classmethod
    def valid_meetup_private_key_path(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("MEETUP_PRIVATE_KEY_PATH must be an absolute private path")
        resolved = path.resolve()
        try:
            resolved.relative_to(ROOT.resolve())
        except ValueError:
            return str(resolved)
        raise ValueError("Meetup private key must be stored outside the repository")

    @field_validator("private_storage_dir")
    @classmethod
    def absolute_private_storage(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("Private storage path must be absolute")
        resolved = value.resolve()
        public_roots = (ROOT / "frontend" / "dist", ROOT / "frontend" / "public")
        if any(
            resolved == root.resolve() or root.resolve() in resolved.parents
            for root in public_roots
        ):
            raise ValueError("Private storage path cannot be inside the frontend public directory")
        return resolved

    @field_validator("allowed_emails")
    @classmethod
    def exact_emails(cls, values: list[str]) -> list[str]:
        values = [v.strip().lower() for v in values]
        if any("@" not in v or "*" in v or not v for v in values):
            raise ValueError("Allowlist must contain exact email addresses")
        if len(set(values)) != len(values):
            raise ValueError("Allowlist contains duplicate addresses")
        return values

    @model_validator(mode="after")
    def secure_configuration(self):
        origin = urlsplit(self.app_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("APP_ORIGIN must be a single origin")
        self.app_origin = self.app_origin.rstrip("/")
        try:
            ZoneInfo(self.business_default_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("EMP_DEFAULT_TIMEZONE must be a valid IANA time zone") from None
        url = make_url(self.database_url.get_secret_value())
        if url.drivername != "postgresql+psycopg":
            raise ValueError("PostgreSQL with psycopg is required")
        if self.dev_auth_enabled:
            raise ValueError("Runtime authentication bypass is not supported")
        if bool(self.google_client_id) != bool(self.google_client_secret.get_secret_value()):
            raise ValueError("Configure both Google client values or neither in development")
        if bool(self.linkedin_client_id) != bool(self.linkedin_client_secret.get_secret_value()):
            raise ValueError("Configure both LinkedIn client values or neither")
        if bool(self.instagram_client_id) != bool(self.instagram_client_secret.get_secret_value()):
            raise ValueError("Configure both Instagram client values or neither")
        if self.extraction_mode == "openai" and not self.openai_api_key.get_secret_value():
            raise ValueError("OpenAI extraction requires OPENAI_API_KEY")
        if self.campaign_generation_mode == "openai":
            if not self.openai_api_key.get_secret_value():
                raise ValueError("OpenAI campaign generation requires OPENAI_API_KEY")
            if not self.openai_campaign_model:
                raise ValueError("OpenAI campaign generation requires OPENAI_CAMPAIGN_MODEL")
        if self.image_generation_mode == "openai" and not self.openai_api_key.get_secret_value():
            raise ValueError("OpenAI image generation requires OPENAI_API_KEY")
        if (
            self.group_recommendation_mode == "openai"
            and not self.openai_api_key.get_secret_value()
        ):
            raise ValueError("OpenAI group recommendations require OPENAI_API_KEY")
        if self.environment == "production":
            if origin.scheme != "https" or origin.hostname in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("Production requires a public HTTPS application origin")
            if not self.google_client_id or not self.allowed_emails:
                raise ValueError("Production requires Google OIDC and an exact allowlist")
            if self.instagram_execution_mode == "LIVE" and (
                not is_external_https_url(self.public_media_base_url)
                or len(self.media_delivery_signing_secret.get_secret_value()) < 32
            ):
                raise ValueError(
                    "LIVE Instagram requires external HTTPS media and a signing secret"
                )
            if self.extraction_mode != "openai":
                raise ValueError("Production requires real OpenAI extraction")
            if self.campaign_generation_mode != "openai":
                raise ValueError("Production requires real OpenAI campaign generation")
            if self.image_generation_mode != "openai":
                raise ValueError("Production requires real OpenAI image generation")
            if self.group_recommendation_mode != "openai":
                raise ValueError("Production requires real OpenAI group recommendations")
            if not url.password or url.password == "local-only-password":
                raise ValueError("Production cannot use local development database credentials")
        elif origin.scheme == "http" and origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("HTTP development/test origins must be loopback")
        if self.environment == "test" and origin.hostname not in {"localhost", "127.0.0.1", "::1"}:
            # Test mode mounts test-only routes and an ephemeral vault key; never deployable.
            raise ValueError("Test environment is restricted to loopback origins")
        return self

    @property
    def business_default_venue(self) -> dict[str, str]:
        return {
            "name": self.business_default_venue_name,
            "street": self.business_default_venue_street,
            "city": self.business_default_venue_city,
            "region": self.business_default_venue_region,
            "postal_code": self.business_default_venue_postal_code,
            "country": "US",
        }

    @property
    def cookie_secure(self) -> bool:
        return self.app_origin.startswith("https://")

    @property
    def session_cookie(self) -> str:
        return "__Host-emp_session" if self.cookie_secure else "emp_session"

    @property
    def flow_cookie(self) -> str:
        return "__Host-emp_login" if self.cookie_secure else "emp_login"

    @property
    def oidc_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret.get_secret_value())
