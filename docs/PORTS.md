# Zenbook Server Port Registry

This file is the source of truth for host ports. New services must reserve a
port here before their Compose configuration is deployed.

| Port | Protocol | Service | Stack |
| ---: | --- | --- | --- |
| 22 | TCP | SSH | host |
| 53 | TCP/UDP | Pi-hole DNS | homelab |
| 80 | TCP | Pi-hole web | homelab |
| 443 | TCP | Pi-hole web | homelab |
| 3000 | TCP | Open WebUI | AI |
| 3001 | TCP | Server-Dash | management |
| 3002 | TCP | Shariah Screener UI | Aegis |
| 3005 | TCP | Finance Dashboard | finance |
| 5432 | TCP | Finance PostgreSQL | finance |
| 5433 | TCP | Aegis PostgreSQL | Aegis |
| 7000 | TCP | Odysseus | AI |
| 8000 | TCP | ReefTracker | Aegis |
| 8001 | TCP | Shariah Screener API | Aegis |
| 8002 | TCP | SRE Agent | Aegis |
| 8003 | TCP | ReefTracker MCP | Aegis |
| 8020 | TCP | Observability MCP | Aegis Core-next |
| 8080 | TCP | E2EE Messenger | Aegis |
| 8443 | TCP | Router proxy | infrastructure |
| 11434 | TCP | Ollama | AI |
| 24454 | UDP | Cobblemon voice | games |
| 25566 | TCP | Cobblemon | games |

Database ports are currently published by the host. Restricting them to
localhost or Tailscale is tracked as a separate migration because changing the
bind address without confirming every client could interrupt access.

The Minecraft entries are current owner-managed reservations, not a fixed
server or world inventory. When a server is created, deleted, or assigned a new
published game/voice port, update this registry before publishing the port. The
Minecraft reference plugin reloads its separate sanitized server registry on
each request, so those owner-directed changes do not require a plugin rebuild or
restart and never grant callers Docker, SSH, RCON, filesystem, or deletion
capabilities.
