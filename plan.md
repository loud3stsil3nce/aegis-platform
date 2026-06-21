MASTER PLAN -- mark as complete after complete.

Phase 1: Bounded Contexts & Data Consolidation (Monorepo Initialization) [COMPLETE] Establish the local development environment using a single repository with isolated microservices. Move away from local JSON/SQLite to a unified, multi-tenant relational database for cross-project state tracking.
Tech Stack: Docker Compose, PostgreSQL 15+, SQLAlchemy/asyncpg, GitHub REST API.
Step 1.1: Network Segregation & Container Provisioning: Configure docker-compose.yml with strict bridge networks (frontend_tier, backend_tier, data_tier). Provision a single PostgreSQL container on the data_tier.
Step 1.2: Database-per-Service Topology & Schema Design: Map isolated logical databases within the Postgres container (strict rule: no cross-schema querying).
db_sre: Tables for AgentLogs, SystemHealth, AuditTrails.
db_screener: Tables for HalalUniverse, ComplianceScans, TradeProposals.
db_reeftracker: Django-managed tables (Users, Aquariums, etc.).
db_e2ee_messenger: App state.
Step 1.3: State Migration & App Integration: Deprecate all SQLite/JSON stores. Refactor sre-agent and shariah-screener to execute CRUD operations via async PostgreSQL drivers (e.g., asyncpg). Configure the Django ReefTracker app to push environment logs to db_reeftracker.
Step 1.4: Stateless SRE Expansion (GitOps Tooling): Expand the SRE agent with an API-driven GitOps module (src/vcs_tools.py). Do not clone repositories locally. Expose tools (get_file_from_api, modify_in_memory, create_branch, commit_via_api, create_pr) using the GitHub REST API and a fine-grained PAT. Enforce a Human-in-the-Loop (HITL) approval gate before PR merges.

Phase 2: Presentation Layer Decoupling (UI Modernization) Retire Streamlit. Implement a production-grade thin client.
Tech Stack: Next.js (React), TypeScript, Tailwind CSS, Vercel.
Step 2.1: Initialize Next.js: Scaffold Next.js application in aegis-platform/frontend (npx create-next-app).
Step 2.2: Build the Control Center UI: Create isolated dashboard routes and Tailwind-styled tabs for "System Health" (/sre), "Shariah Audit" (/finance), and "Reef Logs" (/aquatics).
Step 2.3: API Integration: Implement strictly typed REST/GraphQL fetch utilities in TypeScript to communicate with existing FastAPI/Django backends. Never process business logic on the client.
Step 2.4: The Approval Gate UI: Build a specific "Pending Actions" widget where you can manually click [APPROVE] or [REJECT] for agent-proposed trades or code changes.
Phase 3: Headless Orchestration & MCP Transport Shift Remove Claude Desktop from the loop. Transition the agent from process-bound stdio to distributed networking, running autonomously in the background.
Tech Stack: FastAPI (Python), LangChain/Pydantic AI, APScheduler/Cron, SSE (Server-Sent Events), JSON-RPC 2.0.
Step 3.1: The Runner Service (Transport Shift): Wrap the SRE Agent in a FastAPI server exposing HTTP/SSE endpoints for remote MCP communication. Initialize the LLM via the Anthropic/OpenAI API directly.
Step 3.2: Tool Registration: Programmatically register your MCP servers (SRE, Screener, ReefTracker) to this runner.
Step 3.3: Autonomous Diagnostic Looping (Cron): Implement APScheduler or temporal workflows. Schedule the runner to wake up every 30 minutes, assess SRE health, run compliance checks on a watchlist of stocks, and push results to PostgreSQL.
Step 3.4: Security Gate: Implement programmatic Human-in-the-Loop (HITL) manual overrides for destructive actions (e.g., container restarts, live trading).
Phase 4: Quantitative Engine, Risk Limits & Agentic Memory Finalize financial logic and give your agent the ability to read documentation (AAOIFI, codebase) to debug and propose fixes.
Tech Stack: Pinecone (Vector DB), OpenAI/Anthropic Embeddings API.
Step 4.1: Finalize Financial Math: Finalize AAOIFI compliance math in screener.py.
Step 4.2: Implement Risk Guardrails: Implement portfolio optimizer bounds (e.g., VaR limits, hard asset concentration caps at 10%).
Step 4.3: Ingestion Pipeline: Write a script that parses the AAOIFI PDF standards and your local E2EE app codebase, converts them to vector embeddings, and uploads them to Pinecone.
Step 4.4: Vector Search Tool: Add a tool to your agent: search_knowledge_base(query).
Step 4.5: RAG Implementation: When the agent detects an error in your codebase, force it to query Pinecone for the relevant source code before proposing a fix.
Phase 5: Automated Remediation & Execution Pipelines (The "Hands") Allow the agent to take real-world actions, guarded by math and strict security policies.
Tech Stack: GitHub API (PyGithub), Alpaca API / Interactive Brokers API, Docker SDK.
Step 5.1: Safe Trading: Integrate trade execution APIs strictly gated by Phase 4 compliance output. Hardcode math-based risk constraints (e.g., VaR limits, max 5% portfolio allocation) that the LLM cannot override.
Step 5.2: Code Remediation: Write a tool that allows the agent to checkout a Git branch, modify a file, and open a Pull Request via the GitHub API (never push directly to main).
Step 5.3: Pentesting Sandbox: Create an isolated Docker container for the agent to run synthetic pentests against your E2EE chat app.
Phase 6: Enterprise Observability Scale your telemetry to look like a Big Tech infrastructure pipeline.
Tech Stack: OpenTelemetry (OTel), Prometheus, Grafana, PagerDuty.
Step 6.1: OTel Instrumentation: Add OpenTelemetry traces to your FastAPI and Django backends so you can measure exact function execution times.
Step 6.2: Grafana Dashboards: Connect SRE telemetry to Prometheus/Grafana or Datadog for robust log parsing and alerting. Visualize P95 latency and error rates.
Step 6.3: Incident Alerting: Set up a free PagerDuty tier. Configure Grafana to trigger a PagerDuty phone call if the SRE agent detects a critical container failure.
Phase 7: The Polyglot Pivot (Performance Optimization) Rewrite bottleneck microservices into compiled, statically typed languages to demonstrate mastery of multiple ecosystems.
Tech Stack: Go (Golang), Java (Spring Boot), Maven/Gradle.
Step 7.1: Go for SRE: Port Python SRE Agent to Go for native concurrency and efficient Docker Daemon SDK interactions.
Step 7.2: Java for Finance: Port Shariah Screener backend to Java (Spring Boot) for high-precision financial mathematics and robust OOP.
Step 7.3: Re-link the MCP: Update your Headless Orchestrator (Phase 3) to connect to the new Go and Java binaries instead of the old Python scripts.
Phase 8: Hub-and-Spoke SaaS Deployment (Production) Extract the SRE Control Plane from the Monorepo and deploy it as a multi-tenant, cloud-native SaaS orchestrator using automated CI/CD pipelines.
Tech Stack: AWS (EC2, ECS/Fargate, RDS), API Gateway, Terraform/Pulumi, GitHub Actions, Docker Hub, OIDC/mTLS.
Step 8.1: CI/CD Setup: Write GitHub Actions .yml workflows to automatically test code and build Docker images upon push.
Step 8.2: Multi-Tenancy: Implement Row-Level Security (RLS) and tenant_id columns in the db_sre production database.
Step 8.3: Managed Data Plane: Migrate local Postgres DB to AWS RDS (Relational Database Service).
Step 8.4: Service Discovery: Build dynamic credential and endpoint registration so external platforms can "attach" to the SRE Hub securely.
Step 8.5: Container Hosting: Deploy Next.js frontend to Vercel/Amplify. Deploy backend microservices (Go/Java Headless Orchestrator, SRE Agent, Screener) to AWS ECS Fargate behind an API Gateway so they run 24/7.
Step 8.6: IaC: Automate the entire infrastructure provisioning using Terraform.


Here is the detailed, step-by-step breakdown for Phase 1.

Phase 1: Bounded Contexts & Data Consolidation (Monorepo Initialization)
Step 1.1: Network Segregation & Container Provisioning

Step 1.1.1: Create a root docker-compose.yml file in the aegis-platform directory.

Step 1.1.2: Define three distinct Docker bridge networks in the compose file: frontend_tier, backend_tier, and data_tier.

Step 1.1.3: Add a PostgreSQL service (postgres:15-alpine) to the docker-compose.yml.

Step 1.1.4: Configure the PostgreSQL service to attach exclusively to the data_tier network.

Step 1.1.5: Mount a named volume (postgres_data) to persist database state across container restarts.

Step 1.2: Database-per-Service Topology & Schema Design

Step 1.2.1: Create an initialization script (init.sql) and map it to the /docker-entrypoint-initdb.d/ directory in the PostgreSQL container.

Step 1.2.2: Configure init.sql to execute CREATE DATABASE commands to provision isolated logical databases: db_sre, db_screener, db_reeftracker, and db_e2ee_messenger.

Step 1.2.3: Design the schema for db_sre using SQLAlchemy models (e.g., tables for AgentLogs, SystemHealth, AuditTrails).

Step 1.2.4: Design the schema for db_screener using SQLAlchemy models (e.g., tables for HalalUniverse, ComplianceScans, TradeProposals).

Step 1.3: State Migration & App Integration

Step 1.3.1: Add asynchronous PostgreSQL drivers (asyncpg) to the requirements.txt of the SRE Agent and Shariah Screener projects.

Step 1.3.2: Delete all logic in the SRE Agent that reads or writes to local flat files (e.g., status.json).

Step 1.3.3: Refactor the SRE Agent's Python backend to execute database transactions (CRUD) against db_sre using asyncpg.

Step 1.3.4: Refactor the Shariah Screener's Python backend to execute database transactions against db_screener using asyncpg.

Step 1.3.5: Modify the Django settings in the ReefTracker app to connect to db_reeftracker (via dj-database-url) instead of its local SQLite instance.

Step 1.4: Stateless SRE Expansion (GitOps Tooling)

Step 1.4.1: Create a new module in the SRE Agent called src/vcs_tools.py.

Step 1.4.2: Implement a secure authentication method within the agent to utilize a fine-grained GitHub Personal Access Token (PAT).

Step 1.4.3: Develop the get_file_from_api and modify_in_memory tools using the PyGithub library to manipulate repository files via the REST API (ensuring no local git clone occurs).

Step 1.4.4: Develop the create_branch, commit_via_api, and create_pr tools to push in-memory changes back to GitHub.

Step 1.4.5: Implement a Human-in-the-Loop (HITL) approval gate, ensuring the agent pauses and requests manual confirmation before the create_pr tool executes.


### Phase 1 Refinements & Decoupling Architecture (Added June 2026)
- **Strict Decoupling of Service Databases**: To maintain the plug-and-play capability of the services (especially for later extracting `sre-agent` out of the monorepo), we will avoid centralized models or sharing schema packages (such as `libs/database`) across services.
- **SRE Agent Database Layer (`db_sre`)**: Define local SQLAlchemy models in `services/sre-agent/src/db/models.py` (`AgentLog`, `SystemHealth`, `AuditTrail`).
- **Shariah Screener Database Layer (`db_screener`)**: Define local SQLAlchemy models in `services/shariahcompliantscreener/src/db/models.py` (`Stock`, `AIOverride`, `ManualOverride`, `HalalUniverse`, `DoubtfulUniverse`, `HalalRejection`, `ComplianceScan`, `TradeProposal`).
- **Independent Project Structures**: Ensure both projects act as fully standalone codebases that only connect to their respective logical databases (`db_sre` and `db_screener`).
