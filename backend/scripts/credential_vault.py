"""Explicit maintenance commands for provisioning and rotating vault keys."""

import argparse
import base64
import json
import os
import secrets
import sys
import tempfile
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from app.config import ROOT, Settings
from app.credential_vault import (
    KEY_BYTES,
    KEY_ID,
    CredentialVault,
    CredentialVaultError,
    FileCredentialKeyProvider,
    unique_json_object,
)
from app.db import Database
from app.models import AuditEvent, CredentialEnvelope, ProviderConnection


def _keyring_path(settings) -> str:
    path = settings.credential_vault_keyring_path
    if path is None:
        raise RuntimeError("CREDENTIAL_VAULT_KEYRING_PATH is not configured")
    try:
        path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return str(path.resolve())
    raise RuntimeError("Credential vault keyring must be outside the repository")


def _write_keyring(path: str, key_id: str) -> None:
    if not KEY_ID.fullmatch(key_id):
        raise RuntimeError("Key ID has an invalid format")
    target = os.path.abspath(path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    try:
        with open(target, encoding="utf-8") as source:
            value = json.load(source, object_pairs_hook=unique_json_object)
    except FileNotFoundError:
        value = {"keys": {}}
    except Exception:
        raise RuntimeError("Existing credential keyring is invalid; it was not changed") from None
    keys = value.get("keys") if isinstance(value, dict) else None
    if not isinstance(keys, dict):
        raise RuntimeError("Existing credential keyring is invalid; it was not changed")
    if key_id in keys:
        raise RuntimeError("Key ID already exists; choose a new ID")
    keys[key_id] = base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")
    descriptor, temporary = tempfile.mkstemp(prefix=".credential-keyring-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump({"keys": keys}, output, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class RotationIncomplete(Exception):
    """Rotation stopped part-way; already-committed envelopes remain valid under the new key."""

    def __init__(self, rotated: int, unchanged: int, reason: str):
        super().__init__(reason)
        self.rotated, self.unchanged, self.reason = rotated, unchanged, reason


def _rotate(settings, database=None, vault=None) -> tuple[int, int]:
    if vault is None:
        vault = CredentialVault(
            FileCredentialKeyProvider(
                settings.credential_vault_keyring_path,
                settings.credential_vault_active_key_id,
                ROOT,
            )
        )
    owns_database = database is None
    database = database or Database(settings)
    rotated = unchanged = 0
    try:
        with database.sessions() as read_db:
            identifiers = list(
                read_db.execute(
                    select(CredentialEnvelope.id, CredentialEnvelope.connection_id).order_by(
                        CredentialEnvelope.connection_id
                    )
                )
            )
        for envelope_id, connection_id in identifiers:
            with database.sessions.begin() as db:
                # Lock connection then envelope: the same order as access, refresh,
                # reconnect and disconnect, so rotation cannot deadlock with them.
                connection = db.scalar(
                    select(ProviderConnection)
                    .where(ProviderConnection.id == connection_id)
                    .with_for_update()
                )
                row = db.scalar(
                    select(CredentialEnvelope)
                    .where(
                        CredentialEnvelope.id == envelope_id,
                        CredentialEnvelope.connection_id == connection_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    continue
                if connection is None:
                    raise CredentialVaultError("Credential owner record is unavailable")
                did_rotate = vault.rotate_envelope(db, connection, row)
                if did_rotate:
                    rotated += 1
                    at = datetime.now(UTC)
                    db.add(
                        AuditEvent(
                            id=uuid.uuid4(),
                            actor_type="SYSTEM",
                            actor_id=None,
                            action="CREDENTIAL_KEY_ROTATED",
                            correlation_id=uuid.uuid4(),
                            safe_metadata={"provider": connection.provider},
                            occurred_at=at,
                        )
                    )
                else:
                    unchanged += 1
        return rotated, unchanged
    except Exception as error:
        raise RotationIncomplete(rotated, unchanged, type(error).__name__) from None
    finally:
        if owns_database:
            database.engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate-key", help="add a random key to the external keyring")
    generate.add_argument("--key-id", required=True)
    commands.add_parser("rotate", help="re-encrypt credentials with the configured active key")
    args = parser.parse_args(argv)
    settings = Settings()
    try:
        path = _keyring_path(settings)
        if args.command == "generate-key":
            _write_keyring(path, args.key_id)
            print(json.dumps({"status": "key_written", "key_id": args.key_id, "path": path}))
            print("Set CREDENTIAL_VAULT_ACTIVE_KEY_ID explicitly before deploying this key.")
            return 0
        rotated, unchanged = _rotate(settings)
        print(
            json.dumps({"status": "rotation_complete", "rotated": rotated, "unchanged": unchanged})
        )
        return 0
    except RotationIncomplete as error:
        # Committed envelopes are already re-encrypted; rerunning resumes safely.
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": error.reason,
                    "rotated_before_failure": error.rotated,
                    "unchanged_before_failure": error.unchanged,
                }
            ),
            file=sys.stderr,
        )
        return 1
    except Exception as error:
        # Deliberately report only a bounded category; config/key/provider details stay private.
        print(json.dumps({"status": "failed", "reason": type(error).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
