# Aegis Operations Runbook

## Source of truth

The local Git checkout is the source of truth. The Zenbook Core-next deployment
lives at:

```text
/home/rafiurrahman/projects/aegis-core-next
```

The legacy domain Compose project remains at
`/home/rafiurrahman/projects/aegis-platform`, and independently installable
samples live under `/home/rafiurrahman/projects/aegis-plugins`. Do not edit
deployed source files ad hoc. Validate locally, durably approve and sync the
exact reviewed files, and then recreate only the affected service.

## Required environment

Production secrets belong in the server-side `.env`, with mode `0600`. The
tracked `.env.example` documents required names. Never place real values in
Compose, documentation, or Git.

## Validate before deployment

```bash
docker compose config --quiet
```

Confirm that proposed host ports do not conflict with `docs/PORTS.md`.

## Back up before infrastructure changes

Create PostgreSQL custom-format dumps with `pg_dump -Fc`, store them outside
Docker volumes, checksum them, and perform a restore test into an explicitly
named temporary database. A backup is not considered valid until restoration
has succeeded.

## Deploy one service at a time

```bash
docker compose up -d --no-deps SERVICE
docker compose ps
docker inspect --format '{{.State.Health.Status}}' CONTAINER
docker logs --tail 100 CONTAINER
```

Require the affected container to become healthy before proceeding to another
service. Avoid recreating PostgreSQL during ordinary application deployments.

## Governed restart boundary

The Phase 6 Docker lifecycle proxy is a separate, profile-gated service. It is
not part of Observability MCP and must not join Open WebUI, public backend, or
plugin networks. Configure an independent bearer token and an exact target
allowlist, then start only the proxy with the `governed-restart` profile after
the local contract suites pass. Its API contains only target state and
idempotent restart operations; it has no generic Docker, exec, environment, or
filesystem operation.

The deployed definition is `deploy/compose.deployment.yaml` with protected,
nonsecret paths and IDs in `.env.deployment`. Start or update one component at
a time:

```bash
docker compose --env-file .env.deployment -f deploy/compose.deployment.yaml up -d --no-deps docker-lifecycle
docker compose --env-file .env.deployment -f deploy/compose.deployment.yaml up -d --no-deps deployment-api
```

Rollback is component-scoped: restore the preceding reviewed source/image and
recreate only the failed Phase 6 service. Do not remove the
`aegis-deployment_lifecycle-state` or `aegis-deployment_deployment-state`
volumes; they contain replay protection and durable approval/audit evidence.
Stopping Phase 6 does not require stopping any plugin or Core read-only service.

## Current health endpoints

| Service | Probe |
| --- | --- |
| Screener UI | `http://127.0.0.1:3002/` |
| Screener API | `http://127.0.0.1:8001/docs` |
| ReefTracker | `http://127.0.0.1:8000/` |
| SRE Agent | `http://127.0.0.1:8002/` |
| E2EE Messenger | `http://127.0.0.1:8080/` |
| Observability MCP | `http://100.115.220.54:8020/health` |
| Open WebUI | `http://100.115.220.54:3000/health` |

## Database exposure

The Zenbook `.env` sets `AEGIS_DB_BIND_ADDRESS=100.115.220.54`, restricting
PostgreSQL to Tailscale. The Compose default is loopback. Do not change the
binding to `0.0.0.0`.

## Disk safety

Docker build cache may be pruned when necessary. Do not prune volumes without
mapping every volume to its owner; unused volumes may contain stopped Minecraft
worlds or other retained application data.

## Rollback

Use the baseline configuration and verified database dumps under the protected
server backup directory. Restore configuration first. Restore a database only
when schema/data rollback is actually required; an application-only failure
should normally be rolled back by redeploying its prior image/configuration.
