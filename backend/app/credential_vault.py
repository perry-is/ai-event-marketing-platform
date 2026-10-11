"""Authenticated encryption for provider OAuth credentials at rest."""

import base64
import json
import os
import re
import stat
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CredentialEnvelope

NONCE_BYTES = 12
KEY_BYTES = 32
PAYLOAD_SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 32_768
KEY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


class CredentialVaultError(Exception):
    """Safe-to-handle vault failure; never includes plaintext or key material."""


class CredentialKeyProvider(Protocol):
    def active_key_id(self) -> str: ...

    def key_for(self, key_id: str) -> bytes: ...


class InMemoryCredentialKeyProvider:
    """Explicit test provider. Production must use an external keyring or KMS provider."""

    def __init__(self, keys: dict[str, bytes], active_id: str):
        self._keys = dict(keys)
        self._active_id = active_id
        self._validate()

    def _validate(self):
        if not self._keys or self._active_id not in self._keys:
            raise CredentialVaultError("Credential vault key is unavailable")
        if any(
            not KEY_ID.fullmatch(key) or len(value) != KEY_BYTES
            for key, value in self._keys.items()
        ):
            raise CredentialVaultError("Credential vault keyring is invalid")

    def active_key_id(self) -> str:
        self._validate()
        return self._active_id

    def key_for(self, key_id: str) -> bytes:
        self._validate()
        value = self._keys.get(key_id)
        if value is None:
            raise CredentialVaultError("Credential vault key is unavailable")
        return value


class FileCredentialKeyProvider:
    """Load a secret-mounted JSON keyring whose path is outside the application repo."""

    def __init__(self, path: Path | None, active_id: str, repository_root: Path):
        self.path = path
        self._active_id = active_id
        self.repository_root = repository_root.resolve()
        self._lock = threading.Lock()
        self._loaded: dict[str, bytes] | None = None

    def _load(self) -> dict[str, bytes]:
        if not self.path or not self._active_id or not KEY_ID.fullmatch(self._active_id):
            raise CredentialVaultError("Credential vault key is not configured")
        path = self.path.resolve()
        try:
            path.relative_to(self.repository_root)
        except ValueError:
            pass
        else:
            raise CredentialVaultError("Credential vault keyring must be outside the repository")
        with self._lock:
            if self._loaded is not None:
                return self._loaded
            try:
                if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                    raise ValueError("unsafe keyring permissions")
                raw = json.loads(
                    path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object
                )
                encoded = raw["keys"]
                if not isinstance(encoded, dict) or not encoded:
                    raise ValueError
                keys: dict[str, bytes] = {}
                for key_id, value in encoded.items():
                    if not isinstance(key_id, str) or not KEY_ID.fullmatch(key_id):
                        raise ValueError
                    if not isinstance(value, str):
                        raise ValueError
                    key = base64.b64decode(value, validate=True)
                    if len(key) != KEY_BYTES:
                        raise ValueError
                    keys[key_id] = key
                if self._active_id not in keys:
                    raise ValueError
                self._loaded = keys
                return keys
            except Exception:
                raise CredentialVaultError(
                    "Credential vault keyring is unavailable or invalid"
                ) from None

    def active_key_id(self) -> str:
        self._load()
        return self._active_id

    def key_for(self, key_id: str) -> bytes:
        value = self._load().get(key_id)
        if value is None:
            raise CredentialVaultError("Credential vault key is unavailable")
        return value


def associated_data(
    provider: str,
    connection_id: uuid.UUID,
    envelope_id: uuid.UUID,
    payload_schema_version: int,
    key_id: str,
) -> bytes:
    if provider not in {"LINKEDIN", "INSTAGRAM"} or not KEY_ID.fullmatch(key_id):
        raise CredentialVaultError("Credential context is invalid")
    return (
        f"event-marketing:credential:{provider}:{connection_id}:{envelope_id}:"
        f"{payload_schema_version}:{key_id}"
    ).encode("ascii")


class CredentialVault:
    def __init__(self, key_provider: CredentialKeyProvider):
        self.key_provider = key_provider

    def is_configured(self) -> bool:
        try:
            key_id = self.key_provider.active_key_id()
            return len(self.key_provider.key_for(key_id)) == KEY_BYTES
        except CredentialVaultError:
            return False

    def _encrypt(
        self,
        provider: str,
        connection_id: uuid.UUID,
        envelope_id: uuid.UUID,
        payload: dict,
    ) -> tuple[str, bytes, bytes, int]:
        try:
            key_id = self.key_provider.active_key_id()
            key = self.key_provider.key_for(key_id)
            if len(key) != KEY_BYTES:
                raise CredentialVaultError("Credential vault key is invalid")
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            if len(encoded) > MAX_PAYLOAD_BYTES:
                raise CredentialVaultError("Credential payload is too large")
            version = PAYLOAD_SCHEMA_VERSION
            nonce = os.urandom(NONCE_BYTES)
            ciphertext = AESGCM(key).encrypt(
                nonce,
                encoded,
                associated_data(provider, connection_id, envelope_id, version, key_id),
            )
            return key_id, nonce, ciphertext, version
        except CredentialVaultError:
            raise
        except Exception:
            raise CredentialVaultError("Credential encryption failed") from None

    def store(self, db: Session, connection_id: uuid.UUID, provider: str, payload: dict):
        row = db.scalar(
            select(CredentialEnvelope)
            .where(CredentialEnvelope.connection_id == connection_id)
            .with_for_update()
        )
        envelope_id = row.id if row else uuid.uuid4()
        key_id, nonce, ciphertext, version = self._encrypt(
            provider, connection_id, envelope_id, payload
        )
        at = datetime.now(UTC)
        if row is None:
            row = CredentialEnvelope(
                id=envelope_id,
                connection_id=connection_id,
                key_id=key_id,
                nonce=nonce,
                ciphertext=ciphertext,
                payload_schema_version=version,
                updated_at=at,
            )
            db.add(row)
        else:
            row.key_id = key_id
            row.nonce = nonce
            row.ciphertext = ciphertext
            row.payload_schema_version = version
            row.updated_at = at
        return row

    def retrieve(self, provider: str, connection_id: uuid.UUID, row: CredentialEnvelope) -> dict:
        try:
            if row.payload_schema_version != PAYLOAD_SCHEMA_VERSION:
                raise CredentialVaultError("Credential payload version is unsupported")
            key = self.key_provider.key_for(row.key_id)
            if len(key) != KEY_BYTES or len(row.nonce) != NONCE_BYTES:
                raise CredentialVaultError("Credential envelope is invalid")
            plaintext = AESGCM(key).decrypt(
                row.nonce,
                row.ciphertext,
                associated_data(
                    provider,
                    connection_id,
                    row.id,
                    row.payload_schema_version,
                    row.key_id,
                ),
            )
            value = json.loads(plaintext)
            if not isinstance(value, dict):
                raise CredentialVaultError("Credential payload is invalid")
            return value
        except CredentialVaultError:
            raise
        except (InvalidTag, ValueError, TypeError, json.JSONDecodeError):
            raise CredentialVaultError("Credential envelope authentication failed") from None
        except Exception:
            raise CredentialVaultError("Credential envelope cannot be read") from None

    def rotate_envelope(self, db: Session, connection, row: CredentialEnvelope) -> bool:
        active = self.key_provider.active_key_id()
        if row.key_id == active:
            return False
        payload = self.retrieve(connection.provider, connection.id, row)
        key_id, nonce, ciphertext, version = self._encrypt(
            connection.provider, connection.id, row.id, payload
        )
        row.key_id = key_id
        row.nonce = nonce
        row.ciphertext = ciphertext
        row.payload_schema_version = version
        row.updated_at = datetime.now(UTC)
        return True
