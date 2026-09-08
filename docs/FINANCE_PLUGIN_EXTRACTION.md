# Finance Plugin Extraction Inventory and Threat Model

Status: Phase 8 design baseline (read-only inventory)

Inventory date: 2026-09-04

## Ownership and boundary

- Operational owner: the Aegis installation owner (`loud3stsil3nce` / the
  Zenbook operator). A named long-term code owner must be recorded when the
  source is moved into a version-controlled plugin repository.
- Current source locations are unversioned directory snapshots at
  `/Users/rafiurrahman/Dev/finance-dashboard` and
  `/home/rafiurrahman/Dev/finance-dashboard`. Neither is a Git checkout, so no
  authoritative revision, provenance, release tag, or rollback commit exists.
- Current runtime is a single Compose project containing a Next.js dashboard
  and PostgreSQL. The UI, domain API, Plaid integration, credential management,
  database schema creation, Gemini proxy, and model-directed mutations share
  one application process and trust boundary.
- Target boundary: an optional `finance` managed-container plugin with its own
  repository/package, manifest, service identity, internal domain API/MCP
  adapter, secrets, network, database profile, lifecycle hooks, and conformance
  tests. Aegis Core must import no Finance code or schema.

## Verified live inventory

| Area | Current evidence | Extraction requirement |
| --- | --- | --- |
| Dashboard | `finance-dashboard`, healthy, image ID `sha256:12baf2351fdedc7e1cf7ade979e3d3a99de799a1f77e939548aa2a26bff59168`, host port `3005` on IPv4/IPv6 | Pin an immutable image and limit browser exposure to the intended Tailscale/interface policy |
| Database | `finance-postgres`, healthy, PostgreSQL 16 Alpine, 8,063 kB database, named volume `finance-dashboard_postgres_data`, host port 5432 bound to `100.115.220.54` | Give the plugin a private DB network by default; document an optional convenience profile separately |
| Runtime identity | Dashboard has no configured non-root user; writable root filesystem and writable source, dependency, and build-cache mounts | Non-root UID, read-only root, immutable application image, bounded writable state only, dropped capabilities, no-new-privileges, CPU/memory limits |
| Network | Only dashboard and database share `finance-dashboard_default` | Replace with a plugin-private network; declare narrowly allowlisted Plaid/Gemini egress and no Core/other-plugin reachability |
| Health | Root-page HTTP health check; both containers healthy, restart policy `always` | Separate liveness, readiness, version, and metrics contracts; readiness must include required DB state without exposing records |
| Secrets | Runtime variable names include `DATABASE_URL`; source also supports `ENCRYPTION_SECRET`, Plaid credentials/token, and `GEMINI_API_KEY`; encrypted credentials are stored in `app_credentials` | Separate owner-only secret namespace, fail closed without a strong encryption key, never return stored credentials, rotate credentials during migration |
| Backups | SQL dump `b9fd47...781b0` (mode 0644) and baseline custom dump `a90672...c9ba` (mode 0600) exist; no restore-test evidence was found | Treat the 0644 data dump as overexposed, create encrypted owner-only backups, define retention, and prove restore in an isolated database before cutover |
| Source parity | Key source/config checksums were captured from the server snapshot | Import into Git, establish an authoritative release, and bind deployment/rollback to commit plus immutable image digest |

No Finance service, record, secret, network, volume, or backup was changed while
collecting this inventory.

## Data and schema classification

The current database has eight public tables: `accounts`, `ai_memories`,
`calendar_events`, `goal_account_allocations`, `goal_tracking`,
`peer_to_peer_and_expenses`, `savings_and_assets`, and `user_profile`.
`app_credentials` is created by application startup but was not present in the
live table inventory, indicating schema/runtime drift that must be reconciled.

- Restricted financial data: balances, transactions, account masks,
  institutions, income, goals, allocations, calendar amounts, and profile data.
- Restricted behavioral data: AI memories, chat-derived decisions, notes, and
  financial planning context.
- Secret data: Plaid client secret and access token, Gemini key, database
  credential, and encryption key. Secret data must never enter MCP/model output,
  audit payloads, logs, manifests, or backup metadata.
- Operational metadata: health, version, schema version, sync age, backup age,
  and aggregate counts may be exposed only through bounded, authenticated tools.

The initial schema has primary keys but no declared foreign-key constraints for
account, goal, allocation, transaction, or event relationships. Migration must
add explicit schema versioning and validate referential integrity before adding
constraints or changing data.

## Threat model and current blockers

1. **Unauthenticated disclosure and mutation.** `GET /api/finance/data` returns
   financial records and stored application credentials; its `POST` handler
   accepts account, asset, goal, expense, profile, memory, calendar, and
   credential mutations without authentication, authorization, approval,
   request limits, strict schemas, or audit records.
2. **Credential exposure.** Plaid credentials can arrive in request bodies,
   token exchange returns the encrypted access token to the browser, and all
   application credentials can be returned by the general data endpoint.
3. **Fail-open encryption.** The encryption key falls back to the Plaid secret
   and then to a source-code constant. Decryption failures return the original
   input. The plugin must require a separate strong key and fail closed.
4. **Model-to-mutation path.** The Gemini route places the full financial
   context in an external model request and asks the model to emit mutation
   actions. Browser code parses those actions and calls the unauthenticated
   mutation endpoint. Prompt injection or malformed model output can therefore
   influence stored financial data without policy-layer approval.
5. **Tenant ambiguity.** The schema and Plaid client identifier are effectively
   single-user; records lack an authenticated owner/tenant boundary. Initial
   plugin activation must explicitly remain single-owner or add scoped subject
   identifiers before multi-user use.
6. **Supply-chain and rollback ambiguity.** Mutable local builds, development
   mode, writable bind mounts, unversioned source, and unpinned base images do
   not provide reproducible release or rollback identity.
7. **Backup confidentiality.** One plaintext SQL dump containing financial data
   is readable beyond its owner. Backup encryption, access restriction,
   retention, deletion policy, and isolated restore verification are required.
8. **Availability and isolation.** The application initializes schemas at
   request time, silently falls back from PostgreSQL to local SQLite, and shares
   broad process privileges. A DB outage could create a divergent local database
   rather than fail readiness.

## Capability and risk matrix

| Capability | Risk | Initial plugin policy |
| --- | --- | --- |
| `finance.health`, `finance.version`, `finance.sync_status` | R0 | Authenticated, audited, bounded; no financial values |
| `finance.accounts.summary`, `finance.transactions.list`, `finance.goals.list`, `finance.calendar.list` | R1 | Owner-scoped, redacted, paginated, date/range limits, no credentials |
| `finance.analysis.request`, `finance.plaid.sync` | R2 | Explicit budget/rate limits, egress allowlist, timeout, cancellation, audit; no automatic fallback to a different data store |
| Save/update account, goal, expense, allocation, memory, or calendar data | R4 | Disabled initially; later exact proposal plus separate approval and postcondition |
| Link/unlink institution, exchange/revoke token, rotate credential | R4 | Separate administrative workflow; credentials never cross the model/MCP boundary |
| Delete financial records, database/volume, backups, or reveal credentials | R5 | Prohibited through Aegis tools |

No generic SQL, filesystem, shell, Docker, arbitrary URL, raw credential, or
cross-plugin capability is permitted.

## Target API, audit, and rate-limit contract

- Authenticate every UI API, domain API, MCP request, and lifecycle hook with a
  scoped identity; authorize the subject and capability again at execution.
- Split read DTOs from mutation DTOs. Use strict versioned schemas, reject
  unknown fields, cap request and response bodies, paginate records, and redact
  masks, notes, profile details, and identifiers according to capability.
- Audit request ID, actor, subject, capability, normalized redacted arguments,
  risk, policy version, status, row/result count, external-provider request ID,
  approval ID, and postcondition. Audit failure blocks R2/R4 work.
- Proposed starting limits: R0 60/minute/actor, R1 30/minute/actor with at most
  100 records and a 366-day range, R2 sync/analysis 2/minute and 20/day, and one
  active Plaid sync per installation. Make limits configurable but never absent.
- Treat Plaid/Gemini responses and all financial text as untrusted data. Do not
  allow model output to invoke mutations directly; route it through typed
  proposal, policy, approval, and execution boundaries.

## Lifecycle, backup, and rollback plan

1. Import the source into an authoritative Git repository without committing
   `.env.local`, dumps, build caches, database files, or credentials. Add secret
   scanning and establish the first reviewed baseline commit.
2. Create `aegis-plugin.yaml` with compatibility version, owner, immutable image,
   capabilities, secrets by name, egress destinations, private network,
   database volume/profile, resources, health contracts, and lifecycle hooks.
3. Build a parallel internal Finance API/MCP boundary. Keep all mutation tools
   disabled until auth, policy, audit, redaction, rate-limit, injection, and
   approval tests pass.
4. Produce an encrypted logical backup and verify restore plus application
   readiness in an isolated database. Record schema version, checksum, row-count
   metadata, RTO/RPO, retention, and operator procedure without exposing data.
5. Rehearse install, enable/disable, upgrade permission diff, rollback, and
   removal on a clean Core instance. Removal must preserve the Finance data
   volume by default and leave Core and every other plugin healthy.
6. For production cutover, bind one approved proposal to the exact source commit,
   image digest, schema migration, backup checksum, expected current container,
   health gates, and rollback image/config. Change only Finance, verify UI/API/
   DB health and audit, then either commit the migration or restore the exact
   prior image/config and database state.

## Acceptance evidence still required

- Named code owner and authoritative Git repository/release.
- Versioned manifest and schemas passing the public validator and permission
  diff checks.
- Authenticated and isolated API/MCP identities; no secret or unrestricted
  financial-data path to models.
- Security tests for unauthenticated access, IDOR/tenant crossover, prompt
  injection, SSRF, body/result limits, credential redaction, approval replay,
  and cross-plugin network/database/secret access.
- Contract, conformance, integration, provider-failure, database-outage,
  backup/restore, rollback, and removal tests.
- Clean-Core install and removal proof with all other plugins unchanged.

## Isolated baseline restore evidence

With explicit authorization, the owner-only baseline custom dump (SHA-256
`a906720ffb3df0f249cc4dcefdc67803bb95e6d62ee880315e8492aebf07c9ba`,
20,004 bytes, mode 0600) was restored into a disposable internal-only PostgreSQL
16 container pinned to digest
`sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685`.
The container ran as UID 70 with read-only root, dropped capabilities,
no-new-privileges, tmpfs data, and no port or persistent volume. The dump was
streamed via stdin so its permissions and contents were not changed.

The restore produced eight public tables; the accounts, goal, expense, and
calendar tables and zero-row account/goal query contracts passed. The restored
schema-only SHA-256 was
`65dbd932525600e116d5b6bd97710bcffb23452bc32c75d322e3da8473da62e1`.
No financial values were output. Cleanup removed the disposable container and
network, and both production Finance containers retained exact identity, start
time, and healthy state. This proves baseline restore mechanics only; encrypted
backup creation/retention and post-migration application readiness remain open.

A separately approved fresh backup then streamed `pg_dump -Fc` directly through
GPG AES-256, so no plaintext dump was written. The custom-dump SHA-256 is
`dda68678e4125d75de644c8c2a60391d887b2e4c6d20ec61716c02aed38ec16d`
and ciphertext SHA-256 is
`11406a095efc769ae755ed5cc1c26c44713805b395aae199981472ba7e289b29`.
The backup directory is mode 0700 and ciphertext, passphrase, and metadata are
each mode 0600. Decryption, archive listing, and a second isolated eight-table
restore passed; its schema SHA-256 is
`989a8eeb9b9d526d07c5447fb88b4c7ccdb2a542a316284c5edc4d19d3d36f57`.
Production again remained unchanged. The passphrase is currently colocated with
the ciphertext because no GPG recovery key exists on Zenbook; separation into
an operator secret store remains a pre-cutover remediation.

That separation is now complete: after explicit approval, the unexposed
passphrase moved to owner-only `/home/rafiurrahman/aegis-secrets/finance-backup`
(directory 0700, file 0600), while the backup directory retains only ciphertext
and non-secret metadata. Re-decryption matched the recorded custom-dump hash.
No rotation was necessary because no credential or recovery secret was exposed.

The local alpha now also contains a disconnected PostgreSQL read adapter with
four static SELECT-only, 101-row-capped queries for accounts, transactions,
goals, and calendar records. It rejects the wrong configured owner before
opening a connection, excludes `app_credentials` and mutation SQL, accepts no
caller-selected SQL/table/URL/credential, and returns generic dependency errors.
The total local suite is 21 passing tests. Database permission and runtime
connection remain deliberately absent pending a reviewed candidate manifest.

Candidate `finance@0.1.0-alpha.2` now requests exactly the database URL secret
and private data network as a permission expansion. Exact commit
`335a888ead354e2505fe878a9b858e8c1954879f` builds as non-root image ID
`sha256:69e6d56b0382ad0d50b1032a6c6e615697ca104d81ab24aa6b4ea8f3f26c9702`
from the maintained Python 3.12 slim index digest. Trivy 0.74.0 reports zero
fixed HIGH/CRITICAL findings; an earlier stale base candidate with 35 findings
was blocked and its tag removed.

Against a disposable restore, the candidate passed authenticated account/goal
reads and audit integrity under a read-only, capability-dropped, internal-only
runtime. No financial values were printed. Ephemeral secrets were mode 0400 in
a disposable volume; all test containers, network, volume, and secret files
were removed. Production Finance remained unchanged.

The separate local Finance package baseline and schema-valid read-only manifest
now exist at `/Users/rafiurrahman/Dev/aegis-finance-plugin`. The next
implementation gate is local and reversible: implement the authenticated,
owner-scoped read-only API/MCP service and its security/isolation tests against
fixtures. No live Finance migration is authorized by this document.
