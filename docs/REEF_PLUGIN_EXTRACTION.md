# Reef Phase 8 Plugin Extraction

Status: read-only inventory and threat model complete; production unchanged.

## Provenance and current boundary

- Code owner: `@loud3stsil3nce`.
- Authoritative standalone repository: `loud3stsil3nce/ReefTracker`, clean local
  `main` at `f5656b318c3903851b3ff97dcc6deb58056286aa`.
- The Aegis submodule records the same commit, but its checkout has an
  uncommitted two-line settings change. Extraction must not overwrite or publish
  that change without separate review.
- Production `reeftracker_app` is healthy on image
  `sha256:7ae01dce85c08cc6b6ca411d7d7051eb100d2274ee2702120c11236692b6204f`.
  It runs as root, has a writable root filesystem, bind-mounts mutable source
  read-write, joins frontend/backend/data networks, and publishes port 8000.
- Production `db_reeftracker` is 9223 kB with 28 public tables. Application role
  `reeftracker_app_role` can log in but is not superuser and cannot create
  databases or roles. Credential separation was completed earlier in Phase 8.

## Current data and capabilities

ReefTracker owns users, aquariums, species, livestock, water-parameter readings,
parameter targets, tags, photos, and dosing-product data. The Django UI has
authenticated reads and writes for aquarium, livestock, parameter, target,
photo, tag, profile, and calculator workflows.

The legacy `mcp_server.py` is no longer launched in production. If launched, it
offers two unauthenticated tools: global aquarium listing and livestock lookup
by non-unique aquarium name. Both execute inside the Django/application trust
boundary, omit owner filters and pagination, return free-form strings, disclose
usernames or private notes, and reflect raw exception text. The former SSE
listener binds all interfaces on port 8003.

## Threat model

1. **Cross-tenant disclosure.** Global ORM queries expose every user's aquarium,
   username, livestock, and notes. Aquarium-name lookup can select the wrong
   owner when names collide.
2. **Authentication and authorization absence.** The legacy MCP has no caller
   identity, owner binding, capability checks, or independent service account.
3. **Unbounded output and enumeration.** Whole-table reads have no row, history,
   request, response, or rate bounds.
4. **Sensitive free text and media.** Livestock and parameter notes, photo
   captions/paths, account records, and user identifiers must not cross the
   model boundary by default.
5. **Error disclosure.** Raw database/runtime errors are returned to callers.
6. **Trust-boundary collapse.** Importing Django models gives the MCP the web
   application's full database permissions and dependency graph.
7. **Mutable privileged runtime.** Root execution, a writable root filesystem,
   runtime dependency installation, and a read-write source bind mount prevent
   immutable provenance and enlarge compromise impact.
8. **Undeclared coupling.** Shared networks, a fixed database hostname, and old
   SRE container-name/tool assumptions prevent independent installation.
9. **Missing control evidence.** The legacy MCP has no fail-closed audit, rate
   limit, health/version/metrics contract, backup/restore proof, or lifecycle
   isolation tests.

## Initial capability and risk matrix

| Capability | Risk | Alpha disposition |
| --- | --- | --- |
| Health and version | R0 | Include; no domain data |
| List caller-owned aquariums | R1 | Include; stable IDs, bounded pagination, no usernames |
| Get caller-owned aquarium summary | R1 | Include; strict UUID/integer ID and ownership check |
| List caller-owned livestock | R1 | Include; bounded; omit notes and media paths |
| List recent water parameters | R1 | Include; allowlisted parameter, bounded time/rows |
| Get caller-owned parameter targets | R1 | Include; bounded read |
| Species catalog lookup | R1 | Include; bounded public reference data |
| Notes, photos, captions, tags, account/profile data | R2 | Exclude from alpha |
| Add/edit/delete aquarium, livestock, readings, targets, or media | R3 | Exclude; future durable approval workflow |
| Account/admin, credential, bulk import/export, destructive retention | R4 | Exclude from model-facing MCP |

## Required alpha boundary

- Independent `reef@0.1.0-alpha.1` repository/package; no Core or Django imports.
- Authenticated stateless HTTP/MCP identity with owner and per-capability checks.
- Fixed parameterized PostgreSQL query families using a SELECT-only Reef reader.
- Maximum 100 rows, 366-day history, 8 KiB request, 64 KiB result, strict ID and
  enum schemas, and per-actor rate limits.
- Redact usernames, email/account data, notes, captions, media paths, session
  state, and raw exception details.
- Fail-closed append-only hash-chained audit on dedicated storage.
- Non-root immutable image, read-only root, dropped Linux capabilities,
  no-new-privileges, resource limits, internal-only networks, and no host ports.
- Independent install, enable/disable, permission review, rollback, removal,
  compatibility, backup/restore, database-outage, and cross-plugin isolation
  evidence. Production activation is separately governed and is not required
  for reference-plugin acceptance.

## Rollback and removal

The alpha is additive and read-only. Rollback disables and removes only the Reef
plugin identity/service, restores its prior immutable image if upgraded, and
retains its audit volume according to policy. It must not restart ReefTracker,
alter its database, remove media, or change another plugin/Core registry record.
