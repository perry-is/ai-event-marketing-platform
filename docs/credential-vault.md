# Provider credential vault

WP1M stores the delegated LinkedIn and Instagram OAuth grants needed by the existing live adapters. The vault is narrowly scoped to those provider credentials; it is not a general secret store.

## Threat boundary

The design protects provider tokens from PostgreSQL dump exposure, accidental database inspection, API serialization, ordinary application logs, frontend state, and Git. A database backup by itself contains only encrypted credential envelopes. This does not protect against arbitrary code execution inside the running application, a compromised host, or an attacker who obtains both the database and the active encryption key.

OAuth client IDs, client secrets, Google login credentials, and the vault keyring remain deployment configuration/secrets. The vault key is never stored in PostgreSQL, source code, a container image, or this repository.

## Encryption and record binding

The implementation uses the `cryptography` package's high-level AES-256-GCM AEAD API. Each encryption uses a cryptographically random 12-byte nonce and a 256-bit key. The encrypted JSON payload is bounded and versioned; it contains only the provider access token, optional refresh token, and token type.

The canonical AAD string is:

```text
event-marketing:credential:{provider}:{connection_uuid}:{envelope_uuid}:{payload_schema_version}:{key_id}
```

The database row records the key ID, payload schema version, nonce, ciphertext plus GCM tag, and update timestamp. Provider, owning connection, envelope identity, schema version, and key ID are authenticated. A changed provider or identity, wrong key, damaged nonce, or modified ciphertext fails closed during decryption. ORM representations omit ciphertext and provider-token payloads.

## Key provider and local setup

`CredentialKeyProvider` separates vault operations from key storage. Production configuration uses an absolute `CREDENTIAL_VAULT_KEYRING_PATH` outside the repository and `CREDENTIAL_VAULT_ACTIVE_KEY_ID`. The JSON keyring maps bounded key IDs to Base64-encoded 32-byte keys. Invalid JSON, duplicate keys, invalid IDs, malformed Base64, missing active keys, and keys of the wrong length are rejected. On POSIX systems the provider rejects group/world-readable keyring files. On Windows, protect the containing directory and file with deployment ACLs; the application does not assume POSIX mode-bit behavior.

For a local developer key, choose an external path, set it in `.env`, and run from `backend/`:

```powershell
python -m scripts.credential_vault generate-key --key-id local-2026-10
```

Set `CREDENTIAL_VAULT_ACTIVE_KEY_ID=local-2026-10` and restart the backend. The command writes random key material to the external keyring and prints only the key ID and path. It does not print the key. If the keyring is missing or invalid, connection and credential retrieval fail closed; the app does not auto-create a key at startup. Tests use an explicitly injected in-memory deterministic key provider.

## Rotation and recovery

To rotate, first add a new key with `generate-key`, configure its ID as active, and then run from `backend/`:

```powershell
python -m scripts.credential_vault rotate
```

The command locks each envelope and owning connection, decrypts with the recorded old key, encrypts with a new nonce and active key, atomically updates the envelope in the database transaction, and records safe audit metadata. It reports only counts and never emits plaintext. Keep old keys available until every intended envelope has been re-encrypted and the backup/recovery window has elapsed. A missing historical key makes its envelope unreadable; do not replace that data with an empty or fabricated token. Restore the matching keyring backup or reconnect the provider account.

Database backups must be paired with separately protected keyring backups for recovery. The database alone remains insufficient to recover usable tokens. A key compromise requires retiring/rotating the key and reviewing/revoking affected provider grants. A managed KMS/HSM integration is deferred; it can replace the file key provider without changing the vault's encryption contract.

## Related records

`provider_connections` stores non-secret account identity, owner, granted scopes, expiry, and lifecycle status. `credential_envelopes` stores authenticated ciphertext only. `oauth_authorization_attempts` stores a one-way digest of short-lived authorization state, not raw state or provider authorization codes. See [OAuth connections](oauth-connections.md) for the state machine and provider behavior.
