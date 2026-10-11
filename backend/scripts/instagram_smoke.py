"""Read-only Instagram Login, Professional account, scope, and quota smoke check."""

import json

from app.config import ROOT, Settings
from app.credential_vault import CredentialVault, FileCredentialKeyProvider
from app.db import Database
from app.instagram import InstagramAPIClient, InstagramCredentialGrant, live_readiness
from app.oauth_connections import LiveOAuthGateway, OAuthConnectionError, OAuthConnections


class _SmokeCredentials:
    configured = True

    def __init__(self, grant):
        self.grant = grant

    def access_grant(self):
        return self.grant


def _deferred(reason: str) -> int:
    print(json.dumps({"read_only_smoke": "DEFERRED", "reason": reason, "live_write": "NOT RUN"}))
    return 0


def main() -> int:
    try:
        settings = Settings()
    except Exception:
        return _deferred("Private application configuration is incomplete.")
    if not settings.credential_vault_active_key_id or not settings.credential_vault_keyring_path:
        return _deferred("External credential-vault key configuration is not available.")

    database = Database(settings)
    gateway = LiveOAuthGateway(settings)
    client = InstagramAPIClient(settings)
    try:
        vault = CredentialVault(
            FileCredentialKeyProvider(
                settings.credential_vault_keyring_path,
                settings.credential_vault_active_key_id,
                ROOT,
            )
        )
        connections = OAuthConnections(database, vault, settings, gateway)
        try:
            value = connections.read_only_access_grant("INSTAGRAM")
        except OAuthConnectionError:
            return _deferred(
                "No unexpired encrypted Instagram Professional-account connection is available."
            )
        grant = InstagramCredentialGrant(
            access_token=value["access_token"],
            account_id=value["account_id"],
            username=value["account_name"],
            scopes=frozenset(value["scopes"]),
        )
        smoke_settings = settings.model_copy(update={"instagram_execution_mode": "LIVE"})
        result = live_readiness(smoke_settings, _SmokeCredentials(grant), client)
        status = "PASS" if result["status"] == "READY" and result["checked_live"] else "FAIL"
        print(
            json.dumps(
                {
                    "read_only_smoke": status,
                    "connector_implemented": True,
                    "checked_live": result["checked_live"],
                    "readiness": result["status"],
                    "reason_codes": result["reasons"],
                    "account_eligible": result["account_eligible"],
                    "media_delivery_ready": result["media_delivery_ready"],
                    "live_write": "NOT RUN",
                }
            )
        )
        return 0 if status == "PASS" else 1
    finally:
        client.close()
        gateway.close()
        database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
