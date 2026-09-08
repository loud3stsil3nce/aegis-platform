# Screener Phase 8 extraction inventory and threat model

Status: read-only design; no plugin activation or Screener data mutation is
authorized.  
Prepared: 2026-09-05  
Operational/code owner: `@loud3stsil3nce`

## Provenance and runtime inventory

- Authoritative standalone repository:
  `git@github.com:loud3stsil3nce/shariahcompliantscreener.git`, clean `main` at
  `ad8125916e265f6775ba3106e7b629d501ced8ac`.
- Aegis currently embeds the application as submodule
  `services/shariahcompliantscreener` at different commit
  `04a4d76c6c162421a3e943629632b4f027f39078`. This divergence must be
  reconciled explicitly; Core must not import or own the application source.
- Production backend `shariahscreener` is healthy on mutable local image ID
  `sha256:126a02a2392a80fea9bcccae7a92d34ae8d46817df23fe08727090ed6cfebec2`,
  bind-mounts source read/write at `/app`, publishes host port 8001, and joins
  frontend, backend, and data networks.
- Production UI `shariahscreener_ui` is healthy on mutable local image ID
  `sha256:9210017743cdb779d2669582f440ccb310830aeb893ba63f9167bba58a66f623`,
  bind-mounts source plus mutable dependency/build volumes, and publishes host
  port 3002.
- Database `db_screener` is about 12.7 MB and contains 14 public tables:
  `ai_overrides`, `compliance_scans`, `curated_benchmarks`,
  `doubtful_universe`, `global_segment_patterns`, `halal_rejections`,
  `halal_universe`, `manual_overrides`, `platform_users`,
  `proposed_segment_rules`, `shariah_segment_map`, `stocks`,
  `trade_proposals`, and `watchlist`.
- The 2026-09-05 emergency credential separation moved the backend to distinct
  non-superuser login `shariah_screener_app`, made it owner of only
  `db_screener`, revoked PUBLIC database access, proved all cross-domain
  `CONNECT` checks false, and disabled login for the exposed shared superuser.

## Current capability and trust inventory

The application combines UI, REST, legacy SQLite compatibility, PostgreSQL,
external harvesting, model calls, mutation, and MCP in one trust boundary.
FastAPI currently exposes unauthenticated universe, override, scan, trade,
portfolio, stock, quote, rule, upload, ingestion, screening, audit, and MCP SSE
routes. Mutations include manual/AI override writes, ingestion and upserts,
stock deletion, audits that write overrides, uploads, schema setup, and external
pipeline work. The MCP surface shares this process and has no independent
service identity or capability policy.

Outbound code can contact SEC resources, market-data providers, arbitrary
search results/pages, Gemini, and OpenAI. Some URLs embed provider keys in query
strings. Ticker values are interpolated into SQL in audit paths. Model prompts
include database financial baselines and rule text. Repository data, SEC
filings, web pages, provider responses, uploads, model output, and stored notes
are untrusted content.

## Threat model

1. **Unauthenticated read/write access.** Network reachability currently grants
   domain reads, expensive work, record writes, and deletion without an owner,
   role, capability, request ID, rate limit, or durable audit decision.
2. **MCP privilege collapse.** Read and mutation tools execute inside the same
   process and database identity. Prompt instructions can reach consequences
   without code-layer proposal and approval enforcement.
3. **SQL and identifier injection.** Audit routes interpolate ticker strings;
   compatibility helpers dynamically translate statements and identifiers.
4. **SSRF and hostile-content ingestion.** Search and filing workflows follow
   externally sourced URLs. Retrieved pages can carry prompt injection, large
   bodies, redirects, private-address targets, or malicious file types.
5. **Secret leakage.** A plaintext database fallback was committed and matched
   the former production superuser password. Provider keys appear in process
   environment and sometimes request URLs. Errors, logs, model prompts, or MCP
   results could disclose them.
6. **Cross-domain database/network access.** The legacy shared superuser and
   shared data network crossed Screener, SRE, Reef, and Messaging boundaries.
   Credential separation is complete, but network isolation and independent
   database deployment remain open.
7. **Model-directed financial classification.** Gemini/OpenAI output influences
   compliance findings and stored overrides. Model output is evidence, never an
   authorization or final Shariah ruling.
8. **Unbounded cost and denial of service.** Bulk ingestion, audits, portfolio
   simulations, provider calls, uploads, SSE sessions, and result sets lack a
   unified actor budget and queue/cancellation boundary.
9. **Mutable, root-oriented deployment.** Local build tags, bind-mounted source,
   broad networks, development servers, and unpinned dependencies prevent
   immutable provenance and dependable rollback.
10. **Schema/setup mutation on startup.** Compatibility and setup code can alter
    schema or seed rules, coupling runtime startup to database mutation.

## Initial plugin boundary

Create a separate `aegis-screener-plugin` package. It may depend on the public
Aegis plugin protocol but must not import Core or application modules. Its first
release is a parallel authenticated, owner-scoped, read-only HTTP/MCP adapter
using fixed PostgreSQL queries and a dedicated SELECT-only database role.

Initial capabilities:

| Capability | Risk | Bound result |
| --- | --- | --- |
| `screener.health` | R0 | Plugin/database readiness only |
| `screener.version` | R0 | Plugin, contract, schema, and source identities |
| `screener.summary` | R0 | Counts and last-scan age; no holdings or raw text |
| `screener.universe.list` | R1 | Status-filtered, paginated ticker/name summaries, max 100 |
| `screener.stock.get` | R1 | One ticker's bounded ratios and final status; no raw provider payload |
| `screener.rules.list` | R1 | Paginated normalized rules, max 100; notes treated as untrusted data |
| `screener.scans.list` | R1 | Paginated redacted scan metadata, max 100 and 366 days |

No first-release tool may ingest, screen, audit, upload, optimize, backtest,
delete, write overrides/rules, run caller-selected SQL, fetch a caller-selected
URL, invoke a model/provider, or return raw filings, prompts, provider payloads,
credentials, or unrestricted notes.

## Required controls and tests

- Dedicated service token, actor, owner, exact capability, request ID, policy
  version, rate limit, and append-only fail-closed audit on every request.
- Fixed query allowlist, parameterized ticker/status/date values, strict schemas,
  maximum 100 rows, bounded date windows, pagination, response-size cap, and
  generic dependency errors.
- Separate `screener_plugin_reader` role with CONNECT only to `db_screener`,
  USAGE on approved schema, SELECT only on approved tables/columns, and no
  credential, DDL, DML, function execution, cross-database, or other-plugin
  access.
- Internal-only API and data networks, no host ports, no Core/plugin peer reach,
  read-only root, non-root UID, dropped capabilities, no-new-privileges, bounded
  tmpfs, resource limits, dedicated audit volume, and exactly two secret mounts.
- Regressions for missing/wrong auth, owner crossover, capability substitution,
  SQL/identifier injection, SSRF-shaped arguments, oversized/chunked bodies,
  prompt injection as inert data, secret/error redaction, pagination/range caps,
  audit failure, rate limits, database outage, mutation-method absence, and
  cross-plugin/database/network access.
- Contract/conformance, encrypted backup and isolated restore, lifecycle,
  permission expansion, rollback/removal, and clean-Core isolation evidence.

## Lifecycle and recovery

Install, enable/disable, upgrade, rollback, and removal operate only on the
adapter. Removal preserves the Screener database and audit volume by default.
The existing application continues independently throughout extraction. A
future production migration must bind exact source/image digests, database
schema and backup hashes, current container state, one-service health gate, and
rollback; it requires separate R4 authorization and is not an acceptance
prerequisite for the independent reference-plugin package.
