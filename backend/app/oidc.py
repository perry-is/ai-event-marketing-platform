import secrets
import time
from dataclasses import dataclass

import httpx2
from authlib.integrations.httpx_client import AsyncOAuth2Client
from joserfc import jwt
from joserfc.errors import InvalidKeyIdError
from joserfc.jwk import KeySet

from app.config import Settings

ISSUER = "https://accounts.google.com"
AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    email: str
    display_name: str


class InvalidIdentity(Exception):
    pass


class GoogleOIDC:
    """Only this boundary contacts Google. Tokens never enter application persistence."""

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport  # HTTP boundary substitution in tests, no runtime setting.
        self._keys = None
        self._keys_until = 0.0

    def client(self):
        return AsyncOAuth2Client(
            self.settings.google_client_id,
            self.settings.google_client_secret.get_secret_value(),
            redirect_uri=self.settings.app_origin + "/auth/callback",
            scope="openid email profile",
            code_challenge_method="S256",
            token_endpoint_auth_method="client_secret_post",
            timeout=10,
            transport=self.transport,
            trust_env=False,
        )

    def authorization_url(self, state: str, nonce: str, verifier: str) -> str:
        client = self.client()
        url, _ = client.create_authorization_url(
            AUTHORIZATION_URL,
            state=state,
            nonce=nonce,
            code_verifier=verifier,
            prompt="select_account",
        )
        return url

    async def keys(self, refresh=False):
        if refresh or self._keys is None or time.monotonic() > self._keys_until:
            async with httpx2.AsyncClient(
                timeout=10, transport=self.transport, trust_env=False
            ) as client:
                response = await client.get(JWKS_URL)
                response.raise_for_status()
                self._keys = KeySet.import_key_set(response.json())
                self._keys_until = time.monotonic() + 3600
        return self._keys

    async def exchange(self, code: str, nonce: str, verifier: str) -> Identity:
        try:
            async with self.client() as client:
                token = await client.fetch_token(TOKEN_URL, code=code, code_verifier=verifier)
            encoded = token["id_token"]
            keys = await self.keys()
            # Refresh for Google signing-key rotation; never follow a token-supplied URL.
            try:
                decoded = jwt.decode(encoded, keys, algorithms=["RS256"])
            except InvalidKeyIdError:
                keys = await self.keys(refresh=True)
                decoded = jwt.decode(encoded, keys, algorithms=["RS256"])
            claims = decoded.claims
            jwt.JWTClaimsRegistry(
                leeway=30,
                iss={"essential": True, "values": [ISSUER, "accounts.google.com"]},
                aud={"essential": True, "value": self.settings.google_client_id},
                exp={"essential": True},
                iat={"essential": True},
                sub={"essential": True},
                nonce={"essential": True, "value": nonce},
            ).validate(claims)
            if claims.get("azp", self.settings.google_client_id) != self.settings.google_client_id:
                raise InvalidIdentity()
            if isinstance(claims.get("aud"), list) and len(claims["aud"]) > 1:
                if claims.get("azp") != self.settings.google_client_id:
                    raise InvalidIdentity()
            if (
                claims.get("email_verified") is not True
                or not isinstance(claims.get("email"), str)
                or not isinstance(claims.get("sub"), str)
                or not 1 <= len(claims["sub"]) <= 255
                or not secrets.compare_digest(str(claims.get("nonce", "")), nonce)
            ):
                raise InvalidIdentity()
            if "at_hash" in claims:
                import base64
                import hashlib

                expected = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(token["access_token"].encode()).digest()[:16]
                    )
                    .rstrip(b"=")
                    .decode()
                )
                if not secrets.compare_digest(claims["at_hash"], expected):
                    raise InvalidIdentity()
            return Identity(
                ISSUER,
                claims["sub"],
                claims["email"].strip().lower(),
                str(claims.get("name") or "Event Marketing member")[:200],
            )
        except Exception:
            # Provider exceptions can contain tokens/codes: never log or expose them.
            raise InvalidIdentity() from None
