# Minecraft Management Service Plan

Status: design only. No Minecraft container, world volume, Server-Dash route, or
host configuration is changed by this document.

## Objective

Provide one authenticated Aegis application for the Zenbook-hosted Minecraft
servers. It must let the owner view, create, configure, maintain, back up,
restore, stop, start, and retire servers while preserving the owner's freedom to
create or delete worlds outside Aegis. It is not a generic shell, Docker, SSH,
or RCON interface for an LLM.

## Operating model

The service keeps a durable **server registry** with a stable Aegis server ID,
the container ID/name, image digest, ports, volume IDs, runtime state, and
backup lineage. A host-side inventory reconciler discovers owner-created or
owner-removed servers and updates that registry atomically. The dashboard
therefore shows worlds created through Aegis and worlds created manually; no
world is treated as disposable merely because Aegis did not create it.

The dashboard submits typed lifecycle jobs to a trusted worker. The worker has
a strict operation allowlist and receives an immutable target reference,
expected revision, and a short-lived signed job token. It never executes a
caller-provided command, path, image tag, URL, Docker argument, or RCON command.
The model-facing MCP plugin remains read-only and can only ask for bounded
status, player-count, and backup metadata.

## Components

1. **Minecraft Control API and dashboard** — owner-authenticated UI and API for
   registry views, typed forms, job history, approval prompts, and live status.
2. **Registry and audit store** — database records for servers, worlds/volumes,
   port leases, desired state, immutable artifact provenance, jobs, approvals,
   and append-only audit events.
3. **Inventory reconciler** — read-only discovery of declared Minecraft
   containers and volumes. It imports only records matching the owner-maintained
   discovery policy; it never deletes a container or volume because it is absent.
4. **Lifecycle worker** — separate, tightly scoped host executor. It invokes
   typed, reviewed Docker operations for a registry-selected target and verifies
   pre/postconditions. The API, plugin, and model do not receive Docker-socket,
   SSH, shell, or RCON access.
5. **Backup worker and manifest** — creates versioned, checksum-verified,
   encrypted backups outside the live world volume; emits sanitized metadata for
   the read-only plugin.
6. **Read-only Minecraft MCP plugin** — exposes health, registry-selected
   server status, aggregate player counts, and sanitized backup state only.

## Lifecycle rules

| Operation | Workflow and safeguards |
| --- | --- |
| View | Read-only registry/status query with bounded data; player names, chat, IPs, UUIDs, RCON values, and raw logs are excluded. |
| Create | Typed server profile, explicit image digest/modpack lock, allocated port/volume IDs, dry-run validation, then a durable approved job. |
| Start/stop/restart | Exact registry target and expected state; health gate, timeout, and recorded result. No free-form container name. |
| Edit | Versioned configuration schema and patch preview. Immutable or risky changes use a blue/green replacement or an explicit downtime job; raw `server.properties` editing is not exposed. |
| Maintain | Scheduled health checks, version/provenance drift detection, backup verification, and alerts. Updates require an explicit reviewed artifact and approval. |
| Backup | Quiesce/flush as required by the selected server profile, snapshot/copy to isolated storage, checksum, manifest, and restore drill evidence. |
| Restore | Select a verified backup and target server; create a rollback snapshot first; require owner confirmation tied to exact backup and target; validate before cutover. |
| Retire/delete | Default is **archive/stop and retain**. Destruction of a world volume requires a separate recovery window, an exact inventory of data to be removed, a second explicit owner confirmation, and a documented retention policy. No model-triggered deletion. |

## Delivery phases

1. **Discovery and data model** — inventory current servers/retained worlds
   without altering them; define server profiles, import policy, registry schema,
   port allocation, and backup retention requirements.
2. **Read-only foundation** — deploy registry, reconciler, dashboard inventory,
   audit log, and the existing read-only MCP status plugin. Confirm manually
   created/deleted worlds appear or disappear from the registry safely.
3. **Safe lifecycle minimum** — add create, start, stop, and restart jobs for
   one non-production test server profile. Include locking, idempotency,
   preflight/dry-run, status verification, and rollback/timeout handling.
4. **Configuration and provenance** — add typed configuration edits, immutable
   image/modpack artifacts, compatibility checks, upgrade plans, and maintenance
   jobs. Do not support arbitrary URLs or raw shell commands.
5. **Backups and restore drills** — implement isolated backups, checksums,
   encrypted storage, restore-to-scratch verification, and owner-confirmed
   production restore.
6. **Retirement** — implement archive-first retirement and only then a
   recovery-window deletion flow. Volume deletion remains unavailable until a
   restore drill, retention policy, and explicit owner confirmation flow pass.

## Security and acceptance gates

- Separate least-privilege identities for UI/API, registry, inventory,
  lifecycle, and backup work; no shared main application database password.
- Private network only for control components; no host port for the MCP plugin.
- Non-root, immutable containers, dropped capabilities, resource limits, and
  no-new-privileges wherever the component does not require host authority.
- Every mutation is serialized per server, idempotent, audited, and bound to
  a stable server ID and expected state/revision.
- No existing retained world (`cobblemon_data`, `shared_health_voice_data`,
  `terralith_data`, or `casketofreveries_data`) may be touched during phases 1–2.
- Before any destructive capability is enabled: successful backup, checksum,
  isolated restore drill, recovery-window policy, and owner acceptance test.

## Explicit non-goals

- Reusing the existing Server-Dash SSH/shell routes as the control plane.
- Granting a model generic Docker, SSH, RCON, filesystem, or deletion access.
- Automatically deleting stopped servers, unknown containers, or retained world
  volumes.
