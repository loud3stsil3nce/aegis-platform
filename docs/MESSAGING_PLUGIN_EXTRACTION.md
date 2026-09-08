# Messaging Phase 8 Plugin Extraction

Status: read-only inventory and threat model complete; production unchanged.

## Provenance and current boundary

- Code owner: `@loud3stsil3nce`.
- Authoritative standalone repository: `loud3stsil3nce/E2EE-Messaging-Project`,
  clean local `main` at `fd9aa00705e5cb9ff4941404e22ab7dc83ab7b66`.
- The Aegis submodule records different commit
  `fdd4b8680953d90310f7bff87c37e52dceb1c0bb` and has uncommitted changes to
  `backend/main.py` and `backend/requirements.txt`. Extraction must reconcile
  provenance without overwriting or publishing either checkout.
- Production `e2ee_messenger` is healthy on image
  `sha256:ef4789ba998d46797aa033be14d95fc29a3ba4a2114bc978baf62afde6c859e4`.
  It uses the image default/root identity, writable root filesystem, read-write
  `/app` source bind mount, three shared networks, and public host port 8080.
- `db_e2ee_messenger` is 7703 kB with three public tables. Current counts are
  zero users, zero messages, and zero contacts. Role `e2ee_messenger_app` is a
  non-superuser login that cannot create databases or roles.

## Current data and capabilities

The application stores usernames, password hashes, ECDH/ECDSA public-key JSON,
contact relationships, encrypted-message ciphertext, IVs, signatures, sender
and recipient usernames, and timestamps. The current HTTP/websocket application
allows registration/login key replacement, key lookup, chat-history retrieval,
contact mutation/listing, inbox metadata, and real-time message insertion.

The legacy SSE MCP shares the application process and database identity. It can
enumerate connected usernames, enumerate usernames missing key material, and
load every user or message merely to return global counts. Those endpoints have
no independent authentication, authorization, pagination, rate limit, audit,
or result schema. The legacy MCP transport is disabled in production but remains
in the source checkout.

## Threat model

1. **Identity and social-graph disclosure.** Usernames, contacts, peer pairs,
   inbox metadata, and connected-user lists reveal private relationships.
2. **Cryptographic-material exposure.** Public keys are intentionally public to
   peers but are not operational metadata and must not enter model context.
   Password hashes, encryption secrets, ciphertext, IVs, and signatures are
   always prohibited.
3. **Authentication absence.** HTTP, websocket, and legacy MCP paths trust
   caller-supplied usernames and do not issue or validate durable user sessions.
4. **Impersonation and mutation.** A caller can claim a websocket username,
   submit messages as that username, replace keys during login, or mutate any
   claimed owner's contacts.
5. **Unbounded enumeration.** Chat history, inbox, users, messages, and active
   connections have no row, time, request, response, or rate bounds.
6. **Error/log disclosure.** Raw database errors and connection usernames may be
   emitted to MCP responses or logs; SQLAlchemy echo logging is enabled.
7. **Trust-boundary collapse.** The legacy MCP imports application models,
   websocket state, and the full read/write database identity.
8. **Mutable privileged runtime.** Root/default-user execution, writable root,
   runtime source bind, unpinned base image, and shared networks prevent
   immutable provenance and enlarge compromise impact.
9. **CORS and network exposure.** Wildcard origins with credentials, a public
   host port, and frontend/backend/data network attachment broaden access.
10. **Missing control evidence.** There is no independent fail-closed audit,
    health/version/metrics contract, least-privileged reader, restore proof, or
    lifecycle/cross-plugin isolation test.

## Capability and risk matrix

| Capability | Risk | Alpha disposition |
| --- | --- | --- |
| Health and version | R0 | Include; no account/message data |
| Aggregate service summary | R0 | Include bounded counts only |
| Aggregate key-coverage status | R1 | Include counts only; never usernames or keys |
| Date-bounded delivery volume | R1 | Include time buckets/counts only |
| Date-bounded delivery status | R1 | Include aggregate stored counts; no peers/content |
| Active connection count | R1 | Include count only; no usernames or addresses |
| User/key lookup, contacts, inbox, peers | R2 | Exclude from model-facing alpha |
| Ciphertext, IV, signature, password hash, key material | R4 | Permanently prohibited |
| Send message, add contact, replace keys | R3/R4 | Exclude; requires a separate user-authenticated product workflow |
| Delete message/user/contact or rotate secrets | R4 | Permanently absent from model-facing MCP |

## Required alpha boundary

- Independent `messaging@0.1.0-alpha.1` repository/package; no Core or legacy
  application imports.
- Authenticated stateless HTTP/MCP service identity with exact capability checks.
- Fixed aggregate PostgreSQL queries through a SELECT-only Messaging reader.
- No per-user rows in results. Maximum 366-day windows, 100 time buckets,
  8 KiB request, 64 KiB result, strict date/granularity schemas, and per-actor
  rate limits.
- Redact identities, contacts, addresses, hashes, keys, ciphertext, IVs,
  signatures, secrets, and raw errors.
- Fail-closed hash-chained audit on dedicated storage.
- Non-root immutable image, read-only root, dropped capabilities,
  no-new-privileges, resource limits, internal-only networks, and no host ports.
- Independent install, enable/disable, permission review, rollback, removal,
  compatibility, backup/restore, outage, and cross-plugin isolation evidence.

## Rollback and removal

The alpha is additive and read-only. Rollback disables/removes only the Messaging
plugin identity and adapter, or restores its prior immutable image after an
upgrade. It preserves its audit data according to policy and never restarts the
messaging application, mutates messages/users/contacts, rotates application
keys, or changes another plugin/Core record.
