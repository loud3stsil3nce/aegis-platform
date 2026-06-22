# 🛡️ Aegis Platform

Aegis Platform is a high-performance, containerized monorepo hosting multiple specialized microservices. It features a unified state architecture with database-per-service logical isolation, strict network segregation tiers, and autonomous operator agent looping.

```mermaid
graph TD
    subgraph Frontend Tier (Public Ports)
        UI_Screener[Next.js Screener UI<br>Port 3000]
        UI_Reef[Django ReefTracker UI<br>Port 8000]
        UI_Messenger[Static Messenger UI<br>Port 8080]
    end

    subgraph Backend Tier (Isolated Internal)
        API_Screener[FastAPI Screener Backend<br>Port 8001]
        API_Messenger[FastAPI Messenger Backend<br>Port 8080]
        SRE_Agent[SRE Agent Worker]
    end

    subgraph Data Tier (Protected Internal)
        DB[(aegis_db<br>Postgres 15)]
        DB_SRE[(db_sre)]
        DB_Screener[(db_screener)]
        DB_Reef[(db_reeftracker)]
        DB_Messenger[(db_e2ee_messenger)]
    end

    UI_Screener -->|REST API| API_Screener
    UI_Reef -->|Direct ORM| DB_Reef
    UI_Messenger -->|REST API| API_Messenger

    API_Screener -->|asyncpg| DB_Screener
    API_Messenger -->|asyncpg| DB_Messenger
    SRE_Agent -->|asyncpg| DB_SRE
    SRE_Agent -->|Docker Socket| Daemon[Host Docker Daemon]

    DB -.-> DB_SRE
    DB -.-> DB_Screener
    DB -.-> DB_Reef
    DB -.-> DB_Messenger
```

---

## 📂 Codebase Directory Structure

*   [**`services/`**](file:///home/rafi/projects/aegis-platform/services): Containerized microservices.
    *   [**`sre-agent/`**](file:///home/rafi/projects/aegis-platform/services/sre-agent): Autonomous operations & diagnostic agent. Integrates VCS API-driven GitOps tools (no local git clone) and Docker CLI sockets. Uses `db_sre`.
    *   [**`shariahcompliantscreener/`**](file:///home/rafi/projects/aegis-platform/services/shariahcompliantscreener): Decoupled fintech compliance screener and portfolio optimizer.
        *   `src/`: FastAPI REST API backend on port `8001` interacting with `db_screener`.
        *   `frontend/`: Next.js (TypeScript, Tailwind CSS v4) user dashboard on port `3000`.
    *   [**`reeftracker/`**](file:///home/rafi/projects/aegis-platform/services/reeftracker): Django application for marine aquarium asset tracking, calculators, and accounts. Uses `db_reeftracker`.
    *   [**`e2ee-messenger/`**](file:///home/rafi/projects/aegis-platform/services/e2ee-messenger): End-to-end encrypted messaging service.
        *   `backend/`: FastAPI API backend on port `8080` interacting with `db_e2ee_messenger`.
        *   `frontend/`: Lightweight single-page static HTML interface.
*   [**`libs/`**](file:///home/rafi/projects/aegis-platform/libs): Shared packages.
    *   [`database/`](file:///home/rafi/projects/aegis-platform/libs/database): Historical database package (deprecated in favor of service-local SQLAlchemy definitions to enforce plug-and-play decoupling).
*   [**`alembic/`**](file:///home/rafi/projects/aegis-platform/alembic): Root database migration engine.
*   [`init.sql`](file:///home/rafi/projects/aegis-platform/init.sql): Database bootstrapping script (creates logical databases).
*   [`docker-compose.yml`](file:///home/rafi/projects/aegis-platform/docker-compose.yml): Main Docker composition config.

---

## 🔌 Networking & Security Topology

To enforce strict security boundaries, the platform segregates containers across three isolated bridge networks:

1.  **`frontend_tier`**: Connects user-facing presentation layers (`shariahscreener_ui`, `reeftracker_app`, `e2ee_messenger`) to external ports.
2.  **`backend_tier`**: Allows API microservices (`shariahscreener`, `e2ee_messenger`) and background agents (`sre_agent`) to communicate without exposing core endpoints to the public internet.
3.  **`data_tier`**: A highly restricted database layer. Only backend microservices and database engines attach to this network. **No frontend service is permitted to route directly to `data_tier`.**

---

## 🗄️ Database Architecture

The platform deprecates microservice flat-files (SQLite databases, local JSON logs) and consolidates state into a single PostgreSQL 15 container (`aegis_db`) on host port `5433` (container port `5432`). 

Each service uses isolated schemas and credentials, ensuring **strict database-per-service topology** with zero cross-schema querying:
*   `db_sre`: Stores operations audit trails, agent logs, and docker host daemon execution telemetry.
*   `db_screener`: Stores stock tickers, AI segment disaggregations, compliance scans, and MPT trade proposals.
*   `db_reeftracker`: Django-managed relational database for aquarium metadata and user profiles.
*   `db_e2ee_messenger`: Handles cryptographic keys, user accounts, and ciphertext messaging payloads.

---

## 📊 Phase 1 Baseline Performance Metrics

Captured and verified after Phase 1 Monorepo Consolidation (`baseline.md`):

*   **Clean Build Resolution**: Reduced build times from minutes (due to unpinned sub-dependency conflicts like conflicting `protobuf` versions) to **~17.5 seconds** using a fully pinned `requirements.lock`.
*   **Idle Container Footprint**:
    *   `sre_agent`: 94.53 MiB
    *   `shariahscreener`: 39.11 MiB
    *   `reeftracker_app`: 121.80 MiB
    *   `e2ee_messenger`: 55.79 MiB
    *   `aegis_db`: 98.89 MiB
    *   **Total Overhead**: 410.12 MiB (1.33% idle CPU)
*   **Database Physical Volume**: 112.2 MB total Postgres data folder size.
*   **Test Suite (Shariah Screener)**: 93 passing tests executed inside the container in **196.17 seconds**.

---

## 🛠️ Quick Start & Local Execution

### Prerequisites
*   Docker & Docker Compose
*   An Anthropic API Key (required for `sre_agent`)
*   A Google Gemini or OpenAI API Key (required for `shariahscreener` LLM audits)

### 1. Configure the Environment
Create a `.env` file in the root directory:
```env
ANTHROPIC_API_KEY=your_anthropic_api_key_here
GEMINI_API_KEY=your_gemini_api_key_here
OPENAI_API_KEY=your_openai_api_key_here
ENCRYPTION_KEY=your_e2ee_secret_key_here
```

### 2. Boot the Entire Infrastructure
To build and start all six containers (database, 4 backends, 1 frontend):
```bash
docker-compose up --build
```

### 3. Service Access Points
Once online, you can access the platform services at:
*   **Shariah Screener Dashboard**: [http://localhost:3000](http://localhost:3000)
*   **Shariah Screener API Docs**: [http://localhost:8001/docs](http://localhost:8001/docs)
*   **ReefTracker Interface**: [http://localhost:8000](http://localhost:8000)
*   **E2EE Messenger Chat**: [http://localhost:8080](http://localhost:8080)
*   **PostgreSQL Port**: `localhost:5433` (Username: `Rafiur`, DB: `aegis_platform`)

### 4. Running Test Suites
To run the automated E2E and unit test suite for the Shariah Screener service inside Docker:
```bash
docker exec shariahscreener pytest
```

---

## 🗺️ Development Roadmap (Master Plan)

The platform evolution is detailed in [`plan.md`](file:///home/rafi/projects/aegis-platform/plan.md):

*   **Phase 1: Bounded Contexts & Data Consolidation** [COMPLETE] — Unified DB migration, docker orchestration, and SRE GitOps API modules.
*   **Phase 2: Presentation Layer Decoupling** [COMPLETE] — Separated Streamlit screener into FastAPI (backend) and Next.js (frontend).
*   **Phase 3: Headless Orchestration & MCP Transport Shift** [IN PROGRESS] — Transition SRE Agent into an autonomous background runner using FastAPI HTTP/SSE and programmatically register tool schemas via Model Context Protocol (MCP).
*   **Phase 4: Quantitative Engine & Risk Limits** [PLANNED] — Integrate Pinecone Vector Database, build embedding-driven RAG for AAOIFI compliance documents, and apply asset risk limits.
*   **Phase 5: Automated Remediation & Live Execution** [PLANNED] — Hook up Alpaca API / Interactive Brokers for automated trading, plus pentesting sandboxes.
*   **Phase 6: Enterprise Observability** [PLANNED] — Implement OpenTelemetry (OTel), Prometheus, Grafana, and PagerDuty alert triggers.
*   **Phase 7: Polyglot Pivot** [PLANNED] — Rewrite SRE Agent in Go (native concurrency) and Shariah Screener backend in Java Spring Boot.
*   **Phase 8: Hub-and-Spoke SaaS Deployment** [PLANNED] — Extract control planes into multi-tenant AWS ECS/Fargate + RDS infrastructure managed via Terraform.
