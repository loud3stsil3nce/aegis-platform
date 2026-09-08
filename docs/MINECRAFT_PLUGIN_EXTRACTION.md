# Minecraft Phase 8 Plugin Extraction

Status: read-only inventory and threat model complete; production unchanged.

## Provenance and current boundary

- Code owner: `@loud3stsil3nce`.
- Existing management code lives in private repository
  `loud3stsil3nce/server-dash`, local `main`
  `9c18a36ade7cdc9d3dc06d29384d8d4ac696e54b`. The checkout has unrelated
  uncommitted changes in `server/routes/systemRoutes.js` and `vite.config.js`;
  extraction must preserve them and must not turn Server-Dash into the plugin.
- Active server `cobblemon` is healthy at container ID
  `f3e50c50dcc0835a74afb25e9b56b895f165709c68bb63515eadd5c40b8cb63e`,
  start time `2026-09-03T03:59:50.482430434Z`, and immutable image digest
  `sha256:2b9f121bb539dde1902a1117c2ef5dbb1dfd1283fe242fc1a7a64ba8532b719f`.
  It publishes TCP 25566 and UDP 24454, uses the default bridge, writable root,
  no dropped capabilities/no-new-privileges setting, and read-write named volume
  `cobblemon_data` at `/data`.
- Four retained world volumes exist: `cobblemon_data`,
  `shared_health_voice_data`, `terralith_data`, and
  `casketofreveries_data`. Stopped containers remain for the latter three plus
  the legacy `minecraft` service. They are retained user data and must never be
  cleaned up automatically.
- `/home/rafiurrahman/homelab/minecraft-vanilla-backup` is an old unpacked
  server directory, not a verified restorable archive. Its metadata includes
  RCON configuration files, logs, player cache, world state, properties, and a
  server JAR. No file content was opened. No current Cobblemon backup artifact
  was found.

## Existing management surface

Server-Dash exposes unauthenticated Minecraft HTTP routes backed by generic SSH
shell execution. They list Docker containers and full environment values, create
servers from mutable image tags, list/install mods, add ports by recreating a
container with copied environment strings, read/write raw `server.properties`,
delete containers and named volumes, create backups, and restore by deleting
`/data/*` before extraction. Caller-controlled container names, filenames, URLs,
properties, ports, and other values are interpolated into shell commands.

This surface is not an acceptable plugin adapter and must not be exposed to Core
or a model. The Phase 8 reference plugin is an independent read-only status
service, not a wrapper over Server-Dash, SSH, Docker, RCON, or shell.

## Threat model

1. **Remote command injection.** Multiple route parameters/body fields are
   interpolated into shell commands executed over SSH.
2. **Unauthenticated destructive administration.** Create, recreate, restart,
   property overwrite, restore, container deletion, and world-volume deletion
   are callable without an application authorization boundary.
3. **SSRF/supply-chain execution.** Arbitrary mod/modpack URLs are fetched into
   the world volume and subsequently executed by the game server.
4. **Secret disclosure.** Full Docker environments and retained RCON files can
   expose credentials. Environment values must never enter plugin/model output.
5. **Player privacy.** Logs, player cache, whitelist, bans, IP addresses, chat,
   UUIDs, and operator records are private and require redaction or exclusion.
6. **World integrity.** Restore deletes the live world before validating the
   archive or rollback path. Volume deletion is explicitly prohibited.
7. **Mutable provenance.** `latest`/Java tags are mutable; mods, JARs, and world
   contents can change independently of source review.
8. **Excessive runtime authority.** Docker, SSH, RCON, shell, and writable-world
   access allow arbitrary host/game mutation and cannot be granted to R0/R1 MCP.
9. **Unbounded reads.** Raw properties, logs, mod lists, and backups lack strict
   byte/count/time bounds and may contain attacker-controlled text.
10. **Backup uncertainty.** Retained data exists, but no current, checksum-bound,
    isolated restore proof for the active server was found.

## Capability and risk matrix

| Capability | Risk | Alpha disposition |
| --- | --- | --- |
| Plugin health/version | R0 | Include |
| Allowlisted server health/version | R0 | Include via Minecraft status-ping only |
| Player count/max count | R1 | Include counts only; never names/UUIDs/IPs |
| Bounded redacted operational logs | R1 | Include only through a pre-redacted read-only feed; exclude from initial fixture release |
| Backup status | R1 | Include filename hash, size, timestamp, checksum/verification status; no paths/content |
| Restart or create backup | R3 | Exclude from alpha; future exact durable approval |
| Change properties, install mods, restore world | R4 | Exclude from model-facing MCP |
| Delete world, player, mod, container, or volume | R4 | Permanently prohibited |
| Docker/SSH/RCON/shell/filesystem generic access | R4 | Permanently prohibited |

## Required alpha boundary

- Independent `minecraft@0.1.0-alpha.1` repository/package with no Core or
  Server-Dash imports.
- Authenticated stateless HTTP/MCP service with capability checks and a dynamic,
  sanitized server registry. A trusted out-of-band host inventory reconciler
  atomically adds/removes server IDs as the owner creates or deletes worlds;
  the plugin reloads it per request, so no image rebuild or plugin restart is
  needed. Callers cannot edit the registry or supply a target.
- Minecraft Server List Ping client only: registry-selected host/port, strict
  timeout and response cap, no caller-selected address, RCON, SSH, Docker API,
  shell, or world access.
- Optional backup-status adapter reads a dedicated sanitized manifest generated
  out of band; it never walks caller paths or opens archives/world data.
- Maximum 100 backup records, 8 KiB request, 64 KiB result, strict identifiers,
  generic errors, per-actor rate limits, and prompt-injection-as-data handling.
- Fail-closed hash-chained audit on dedicated storage.
- Non-root immutable image, read-only root, dropped capabilities,
  no-new-privileges, resource limits, internal management network, and no host
  ports. Game egress is restricted to the one declared server endpoint.
- Independent install, enable/disable, permission review, rollback, removal,
  compatibility, outage, sanitized-manifest backup/restore evidence, and
  cross-plugin isolation tests.

## Rollback and removal

The alpha is additive and read-only. Owner-driven server/world creation or
deletion is reflected by the trusted registry reconciler and remains independent
of plugin lifecycle. Rollback or removal affects only the plugin
identity, adapter image, and audit storage policy. It never stops/restarts a game
server, touches a world volume, changes properties/mods/players, deletes retained
containers, or modifies Server-Dash. Future R3 restart/backup operations require
exact immutable target binding, a one-time approval, health gate, and rollback;
world/player/mod deletion remains prohibited.
