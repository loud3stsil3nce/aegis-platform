# How pgvector Works in Your Setup

## The 30-Second Version

pgvector is a **PostgreSQL extension** — not a separate service. It adds a new column type called `vector` to Postgres, so you can store and search embeddings **right next to your regular data** in `db_sre`. No extra containers, no extra API keys, no extra network hops.

---

## What Changed in Your Architecture

### Before (Pinecone plan)
```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│  SRE Agent   │────▶│  OpenAI API  │     │  Pinecone Cloud  │
│  (port 8002) │     │  (embeddings)│     │  (vector search) │
└──────────────┘     └──────────────┘     └──────────────────┘
       │                                          ▲
       │              NETWORK CALL                │
       └──────────────────────────────────────────┘
       Two external services. Two API keys. Latency on every query.
```

### After (pgvector)
```
┌──────────────┐     ┌──────────────┐     ┌───────────────────────────┐
│  SRE Agent   │────▶│  OpenAI API  │     │  aegis_db (PostgreSQL 15) │
│  (port 8002) │     │  (embeddings)│     │  ┌─────────────────────┐  │
└──────┬───────┘     └──────────────┘     │  │ db_sre              │  │
       │                                  │  │  ├─ agent_logs       │  │
       │         SAME DB CONNECTION       │  │  ├─ system_health    │  │
       └─────────────────────────────────▶│  │  ├─ audit_trails     │  │
                                          │  │  ├─ knowledge_vectors│◀── NEW
                                          │  │  └─ knowledge_       │  │
                                          │  │     ingestion_runs   │◀── NEW
                                          │  └─────────────────────┘  │
                                          │  ┌─────────────────────┐  │
                                          │  │ db_screener         │  │
                                          │  │ db_reeftracker      │  │
                                          │  │ db_e2ee_messenger   │  │
                                          │  └─────────────────────┘  │
                                          └───────────────────────────┘
       One container. Same connection string. Vectors live alongside your data.
```

The SRE agent already connects to `db_sre` at `postgresql+asyncpg://Rafiur:Rafiur123@db:5432/db_sre`. pgvector uses **that same connection** — no new credentials, no new ports.

---

## The Docker Image Swap

The only infrastructure change is one line in `docker-compose.yml`:

```diff
-    image: postgres:15-alpine
+    image: pgvector/pgvector:pg15
```

`pgvector/pgvector:pg15` is the **exact same PostgreSQL 15** image, but with the `vector` extension pre-compiled and installed. Your existing databases, users, volumes, healthchecks — all unchanged. It's a drop-in replacement.

---

## How `vector(1536)` Works

### What is a vector?

When OpenAI's `text-embedding-3-small` model reads a chunk of text, it outputs a list of **1,536 floating-point numbers**. This list is a "vector" — a point in 1,536-dimensional space where **similar meanings are close together**.

```python
# Example: embedding the text "AAOIFI debt ratio must not exceed 30%"
from openai import OpenAI
client = OpenAI()
response = client.embeddings.create(
    input="AAOIFI debt ratio must not exceed 30%",
    model="text-embedding-3-small"
)
vector = response.data[0].embedding
# vector = [0.0023, -0.0145, 0.0312, ..., -0.0089]  # 1,536 floats
```

### How Postgres stores it

In standard Postgres, you'd have no way to store this. pgvector adds the `vector` column type:

```sql
-- This is in your init.sql
CREATE TABLE knowledge_vectors (
    id VARCHAR(512) PRIMARY KEY,
    embedding vector(1536) NOT NULL,   -- ← THIS IS THE NEW TYPE
    source VARCHAR(100) NOT NULL,       -- "AAOIFI_Standard_21" or "codebase"
    file_path TEXT,                     -- e.g. "sre-agent/src/health.py"
    symbol VARCHAR(255),                -- e.g. "register_health_tools"
    page INTEGER,                       -- PDF page number (for AAOIFI)
    chunk INTEGER,                      -- chunk index within a page
    text_content TEXT,                   -- the original text (for retrieval)
    created_at TIMESTAMP DEFAULT NOW()
);
```

`vector(1536)` means "a fixed-length array of 1,536 floats". Postgres stores it compactly as a binary blob — roughly **6 KB per row** (1536 × 4 bytes). For your codebase (~200 symbols) + AAOIFI PDF (~50 chunks), that's about **1.5 MB total**. Trivial compared to your current 112 MB database.

---

## The Full Data Pipeline

### Step 1 — Ingestion (one-time, or periodic)

#### AAOIFI PDF Ingestion
```
python scripts/ingest_aaoifi.py /path/to/aaoifi_standard_21.pdf
```

```
📄 AAOIFI PDF
    │
    ▼ pdfplumber (extract text page-by-page)
Page 1 text, Page 2 text, ...
    │
    ▼ tiktoken (500-token windows, 50-token overlap)
Chunk 0, Chunk 1, Chunk 2, ...
    │
    ▼ OpenAI API (text-embedding-3-small)
[0.002, -0.014, ...], [0.031, 0.008, ...], ...
    │
    ▼ SQL INSERT / ON CONFLICT DO UPDATE
db_sre.knowledge_vectors
```

Each chunk gets an ID like `aaoifi_p3_c1` (page 3, chunk 1) and metadata: source, page, chunk index, plus the original text for retrieval.

#### Codebase Ingestion
```
python scripts/ingest_codebase.py --root /home/rafi/projects/aegis-platform/services
```

```
📁 services/*.py files
    │
    ▼ ast.parse (extract top-level functions & classes)
register_health_tools(), calculate_var(), run_screener(), ...
    │
    ▼ OpenAI API (text-embedding-3-small)
[0.012, -0.003, ...], [0.045, 0.021, ...], ...
    │
    ▼ SQL INSERT / ON CONFLICT DO UPDATE
db_sre.knowledge_vectors
```

Each symbol gets an ID like `code_sre_agent_src_health_py_register_health_tools` and metadata: file path + symbol name.

### Step 2 — Querying (at runtime, by the SRE agent)

When the agent needs to search the knowledge base (Step 4.4, coming next), it will run a query like this:

```sql
-- "Give me the 5 most relevant chunks to this error"
SELECT id, source, file_path, symbol, page, text_content,
       embedding <=> $1 AS distance          -- ← cosine distance operator
FROM knowledge_vectors
ORDER BY embedding <=> $1                    -- ← sort by similarity
LIMIT 5;
```

Where `$1` is the **embedding of the search query** (e.g., the agent embeds the error message "division by zero in screener.py" and passes that vector).

### How `<=>` Works

| Operator | Meaning | Use Case |
|----------|---------|----------|
| `<=>` | **Cosine distance** | What we use. 0 = identical meaning, 2 = opposite. |
| `<->` | L2 (Euclidean) distance | Alternative metric, not used here. |
| `<#>` | Negative inner product | Alternative metric, not used here. |

Cosine distance is ideal for text embeddings because it measures the **angle** between vectors (semantic similarity) rather than magnitude. Two chunks about "debt ratios" will have a small cosine distance even if one is a long paragraph and the other is a short sentence.

---

## The HNSW Index — Why Search is Fast

Without an index, Postgres would do a **sequential scan** — compare the query vector to every single row. Fine for 250 rows, brutal for 100,000+.

The HNSW (Hierarchical Navigable Small World) index in `init.sql` pre-builds a graph structure:

```sql
CREATE INDEX idx_knowledge_vectors_embedding
    ON knowledge_vectors
    USING hnsw (embedding vector_cosine_ops);
```

```
                    ┌─────┐
          Layer 2   │  A  │ ← sparse (few nodes, long-range connections)
                    └──┬──┘
                  ┌────┴────┐
          Layer 1 │  A   B  │ ← medium
                  └──┬───┬──┘
              ┌──────┴───┴──────┐
      Layer 0 │ A  B  C  D  E  │ ← dense (all nodes, short-range connections)
              └─────────────────┘

Search starts at the top layer, greedily descends through
layers, narrowing candidates at each level. O(log n) vs O(n).
```

For your scale (~250-500 vectors), the index is marginal. But it's free to create and will matter if you later ingest thousands of documents.

---

## How It Fits Into the Agent Loop

This is the flow that Steps 4.4 and 4.5 will implement:

```
1. Agent calls run_screener_scan()
2. Tool returns: "Error: division by zero in screener.py"

   ── RAG rule triggers: error detected → must search first ──

3. Agent calls OpenAI: embed("division by zero in screener.py")
   → returns [0.012, -0.003, ..., 0.045] (1536 floats)

4. Agent queries db_sre:
   SELECT * FROM knowledge_vectors
   ORDER BY embedding <=> $query_vector
   LIMIT 5

   → Returns:
     1. screener.py → run_screener() (codebase)
     2. AAOIFI p12 chunk 3 (standard)
     3. optimizer.py → calculate_var() (codebase)

5. Agent sends to LLM:
   "Here's the error + relevant code + AAOIFI context. Propose a fix."

6. LLM responds:
   "The division by zero occurs when market_cap is 0. Add a guard..."
```

---

## Practical Commands

### Rebuild with pgvector (you'll need to do this once)
```bash
docker compose down -v          # -v wipes the DB volume (required since image changed)
docker compose up -d --build    # rebuilds all containers with new image + deps
```

### Verify pgvector is working
```bash
docker exec -it aegis_db psql -U Rafiur -d db_sre -c "SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';"
```
Expected output:
```
 extname | extversion
---------+------------
 vector  | 0.8.0
```

### Check the knowledge_vectors table
```bash
docker exec -it aegis_db psql -U Rafiur -d db_sre -c "SELECT COUNT(*) FROM knowledge_vectors;"
```
(Will be 0 until you run the ingestion scripts)

### Run ingestion (inside the sre-agent container)
```bash
# After placing the AAOIFI PDF somewhere accessible:
docker exec -it sre_agent python scripts/ingest_aaoifi.py /app/code/path/to/aaoifi.pdf

# For codebase:
docker exec -it sre_agent python scripts/ingest_codebase.py --root /app/code/services
```

---

## Key Advantage Over Pinecone

| | Pinecone | pgvector (your setup) |
|---|---|---|
| **Extra service** | Yes (cloud API) | No — same Postgres container |
| **API key** | Required (`PINECONE_API_KEY`) | Not needed |
| **Latency** | Network round-trip to cloud | Local socket to `aegis_db` |
| **Cost** | Free tier has limits | Free forever |
| **Data locality** | Vectors in US-East-1 | Vectors next to your `agent_logs` |
| **Backup** | Separate backup strategy | Backed up with `postgres_data` volume |
| **JOINs with your data** | Impossible | `JOIN agent_logs ON ...` works! |

The JOIN capability is the real killer feature. In Step 4.5, the agent could do things like: "find the most relevant AAOIFI standard for the last error I logged" — a single SQL query joining `agent_logs` with `knowledge_vectors` via a vector similarity subquery. Pinecone can never do that.
