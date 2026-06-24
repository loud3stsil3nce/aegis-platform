#!/usr/bin/env python3
"""
Codebase Ingestion Pipeline

Walks the services/ directory, parses Python files using ast to extract
top-level functions and classes, generates embeddings via OpenAI
text-embedding-3-small, and upserts vectors to the pgvector knowledge_vectors
table in db_sre.

Usage:
    python scripts/ingest_codebase.py                              # default: /app/code/services
    python scripts/ingest_codebase.py --root /path/to/services     # custom root

Environment Variables:
    OPENAI_API_KEY  - OpenAI API key for embeddings
    DATABASE_URL    - PostgreSQL connection string for db_sre
"""

import os
import sys
import ast
import argparse
from openai import OpenAI
from sqlalchemy import create_engine, text


# Directories to skip during traversal
SKIP_DIRS = {'venv', '.venv', '__pycache__', 'node_modules', '.git', '.next'}


# ---------------------------------------------------------------------------
# File Discovery
# ---------------------------------------------------------------------------

def walk_python_files(root_dir: str) -> list[str]:
    """Recursively find all .py files under root_dir, skipping irrelevant dirs."""
    py_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            if f.endswith('.py'):
                py_files.append(os.path.join(dirpath, f))
    return sorted(py_files)


# ---------------------------------------------------------------------------
# AST Symbol Extraction
# ---------------------------------------------------------------------------

def extract_symbols(filepath: str) -> list[dict]:
    """Parse a Python file and extract top-level functions and classes.

    Returns list of {'symbol': str, 'code': str}.
    """
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        source = f.read()

    if not source.strip():
        return []

    try:
        tree = ast.parse(source)
    except SyntaxError:
        # If the file can't be parsed, treat the whole file as one chunk
        return [{'symbol': '__module__', 'code': source}]

    symbols = []
    lines = source.split('\n')

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno - 1
            end = getattr(node, 'end_lineno', None) or start + 1
            code = '\n'.join(lines[start:end])
            symbols.append({'symbol': node.name, 'code': code})

    # If no top-level symbols found, treat the whole file as one chunk
    if not symbols:
        symbols.append({'symbol': '__module__', 'code': source})

    return symbols


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def generate_embeddings(texts: list[str], batch_size: int = 100) -> list[list[float]]:
    """Generate embeddings via OpenAI text-embedding-3-small in batches."""
    client = OpenAI()
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        # Truncate very long code blocks to avoid token limits
        batch = [t[:8000] for t in batch]
        response = client.embeddings.create(input=batch, model="text-embedding-3-small")
        batch_embeddings = [item.embedding for item in response.data]
        all_embeddings.extend(batch_embeddings)
        print(f"  Embedded batch {i // batch_size + 1} ({len(batch)} chunks)")
    return all_embeddings


# ---------------------------------------------------------------------------
# pgvector Upsert
# ---------------------------------------------------------------------------

def upsert_to_pgvector(engine, vectors: list[dict], batch_size: int = 100):
    """Upsert vectors to the knowledge_vectors table in batches."""
    with engine.connect() as conn:
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            for vec in batch:
                conn.execute(text("""
                    INSERT INTO knowledge_vectors
                        (id, embedding, source, file_path, symbol, text_content)
                    VALUES (:id, :embedding, :source, :file_path, :symbol, :text_content)
                    ON CONFLICT (id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        file_path = EXCLUDED.file_path,
                        symbol = EXCLUDED.symbol,
                        text_content = EXCLUDED.text_content
                """), {
                    "id": vec["id"],
                    "embedding": vec["embedding"],
                    "source": vec["source"],
                    "file_path": vec["file_path"],
                    "symbol": vec["symbol"],
                    "text_content": vec["text_content"],
                })
            conn.commit()
            print(f"  Upserted batch {i // batch_size + 1} ({len(batch)} vectors)")


# ---------------------------------------------------------------------------
# Database Logging
# ---------------------------------------------------------------------------

def log_ingestion_run(engine, source_type: str, source_path: str,
                      chunks_upserted: int, status: str, error_message: str = None):
    """Write a KnowledgeIngestionRun row to db_sre."""
    with engine.connect() as conn:
        conn.execute(text(
            "INSERT INTO knowledge_ingestion_runs "
            "(source_type, source_path, chunks_upserted, status, error_message) "
            "VALUES (:st, :sp, :cu, :s, :em)"
        ), {"st": source_type, "sp": source_path, "cu": chunks_upserted,
            "s": status, "em": error_message})
        conn.commit()
    print(f"  Logged ingestion run: {status}")


# ---------------------------------------------------------------------------
# ID Sanitization
# ---------------------------------------------------------------------------

def sanitize_id(filepath: str, symbol: str) -> str:
    """Create a Postgres-safe vector ID from filepath and symbol name."""
    sanitized = filepath.replace('/', '_').replace('.', '_').replace('-', '_')
    return f"code_{sanitized}_{symbol}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ingest Python codebase into pgvector knowledge base."
    )
    parser.add_argument(
        "--root", default="/app/code/services",
        help="Root directory to walk for Python files (default: /app/code/services)"
    )
    args = parser.parse_args()

    root_dir = args.root
    if not os.path.isdir(root_dir):
        print(f"ERROR: Directory not found: {root_dir}")
        sys.exit(1)

    # Validate environment
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY environment variable not set.")
        sys.exit(1)

    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)

    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(sync_url)

    total_chunks = 0
    try:
        # 1. Discover Python files
        print(f"Scanning: {root_dir}")
        py_files = walk_python_files(root_dir)
        print(f"  Found {len(py_files)} Python files.")

        # 2. Extract symbols from each file
        print("Extracting symbols...")
        all_chunks = []  # list of (vector_id, code_text, metadata)
        for filepath in py_files:
            rel_path = os.path.relpath(filepath, root_dir)
            symbols = extract_symbols(filepath)
            for sym in symbols:
                vec_id = sanitize_id(rel_path, sym['symbol'])
                all_chunks.append({
                    "id": vec_id,
                    "code": sym['code'],
                    "source": "codebase",
                    "file_path": rel_path,
                    "symbol": sym['symbol'],
                })

        total_chunks = len(all_chunks)
        print(f"  Extracted {total_chunks} code chunks from {len(py_files)} files.")

        if total_chunks == 0:
            print("WARNING: No code chunks extracted.")
            log_ingestion_run(engine, "codebase", root_dir, 0, "success",
                              "No Python symbols found.")
            return

        # 3. Generate embeddings
        print("Generating embeddings...")
        chunk_texts = [c["code"] for c in all_chunks]
        embeddings = generate_embeddings(chunk_texts)

        # 4. Build vectors and upsert
        print("Upserting to pgvector...")
        vectors = []
        for chunk_data, embedding in zip(all_chunks, embeddings):
            vectors.append({
                "id": chunk_data["id"],
                "embedding": str(embedding),  # pgvector accepts string representation
                "source": chunk_data["source"],
                "file_path": chunk_data["file_path"],
                "symbol": chunk_data["symbol"],
                "text_content": chunk_data["code"][:2000],  # Truncate for storage
            })
        upsert_to_pgvector(engine, vectors)

        # 5. Log success
        print(f"\nIngestion complete: {total_chunks} chunks upserted from {len(py_files)} files.")
        log_ingestion_run(engine, "codebase", root_dir, total_chunks, "success")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"\nERROR: Ingestion failed: {error_msg}")
        log_ingestion_run(engine, "codebase", root_dir, total_chunks, "failed", error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
