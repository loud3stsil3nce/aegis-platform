#!/usr/bin/env python3
"""
pgvector Setup Script

Verifies that the pgvector extension and knowledge_vectors table exist
in db_sre. This is idempotent — safe to run multiple times.

Usage:
    python scripts/setup_pgvector.py

Environment Variables:
    DATABASE_URL  - PostgreSQL connection string (e.g. postgresql+asyncpg://...)
"""

import os
import sys
from sqlalchemy import create_engine, text


def setup_pgvector():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        print("ERROR: DATABASE_URL environment variable not set.")
        sys.exit(1)

    # Convert async URL to sync for this one-off script
    sync_url = db_url.replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(sync_url)

    with engine.connect() as conn:
        # 1. Enable pgvector extension
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
        print("✓ pgvector extension enabled.")

        # 2. Create knowledge_vectors table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS knowledge_vectors (
                id VARCHAR(512) PRIMARY KEY,
                embedding vector(1536) NOT NULL,
                source VARCHAR(100) NOT NULL,
                file_path TEXT,
                symbol VARCHAR(255),
                page INTEGER,
                chunk INTEGER,
                text_content TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """))
        print("✓ knowledge_vectors table ready.")

        # 3. Create HNSW index for cosine similarity
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_knowledge_vectors_embedding
                ON knowledge_vectors
                USING hnsw (embedding vector_cosine_ops);
        """))
        print("✓ HNSW cosine index ready.")

        # 4. Create knowledge_ingestion_runs table
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS knowledge_ingestion_runs (
                id SERIAL PRIMARY KEY,
                source_type VARCHAR(50) NOT NULL,
                source_path TEXT NOT NULL,
                chunks_upserted INTEGER NOT NULL,
                run_at TIMESTAMP DEFAULT NOW(),
                status VARCHAR(20) NOT NULL,
                error_message TEXT
            );
        """))
        print("✓ knowledge_ingestion_runs table ready.")

        conn.commit()

        # 5. Print stats
        result = conn.execute(text("SELECT COUNT(*) FROM knowledge_vectors;"))
        count = result.scalar()
        print(f"\nCurrent vector count: {count}")

    print("\npgvector setup complete.")


if __name__ == "__main__":
    setup_pgvector()
