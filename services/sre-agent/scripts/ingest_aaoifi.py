#!/usr/bin/env python3
"""
AAOIFI Standard PDF Ingestion Pipeline

Parses AAOIFI Standard No. 21 (or other standards) PDFs, chunks text into
~500-token windows with 50-token overlap, generates embeddings via OpenAI
text-embedding-3-small, and upserts vectors to the pgvector knowledge_vectors
table in db_sre.

Usage:
    python scripts/ingest_aaoifi.py /path/to/aaoifi_standard_21.pdf

Environment Variables:
    OPENAI_API_KEY  - OpenAI API key for embeddings
    DATABASE_URL    - PostgreSQL connection string for db_sre
"""

import os
import sys
import argparse
import tiktoken
import pdfplumber
from openai import OpenAI
from sqlalchemy import create_engine, text


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text_content: str, max_tokens: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping token windows."""
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode(text_content)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk_tokens = tokens[start:end]
        chunks.append(enc.decode(chunk_tokens))
        start += max_tokens - overlap
    return chunks


# ---------------------------------------------------------------------------
# PDF Extraction
# ---------------------------------------------------------------------------

def extract_pdf_pages(pdf_path: str) -> list[dict]:
    """Extract text from each page of a PDF.
    Returns list of {"page": int, "text": str}."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            page_text = page.extract_text() or ""
            if page_text.strip():
                pages.append({"page": i, "text": page_text})
    return pages


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def generate_embeddings(texts: list[str], batch_size: int = 100) -> list[list[float]]:
    """Generate embeddings via OpenAI text-embedding-3-small in batches."""
    client = OpenAI()
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
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
                    INSERT INTO knowledge_vectors (id, embedding, source, page, chunk, text_content)
                    VALUES (:id, :embedding, :source, :page, :chunk, :text_content)
                    ON CONFLICT (id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        text_content = EXCLUDED.text_content
                """), {
                    "id": vec["id"],
                    "embedding": vec["embedding"],
                    "source": vec["source"],
                    "page": vec["page"],
                    "chunk": vec["chunk"],
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
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Ingest AAOIFI Standard PDF into pgvector knowledge base."
    )
    parser.add_argument("pdf_path", help="Path to the AAOIFI Standard PDF file.")
    args = parser.parse_args()

    pdf_path = args.pdf_path
    if not os.path.isfile(pdf_path):
        print(f"ERROR: File not found: {pdf_path}")
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
        # 1. Extract PDF pages
        print(f"Extracting text from: {pdf_path}")
        pages = extract_pdf_pages(pdf_path)
        print(f"  Extracted {len(pages)} pages with text.")

        # 2. Chunk all pages
        print("Chunking text...")
        all_chunks = []  # list of dicts
        for page_info in pages:
            page_chunks = chunk_text(page_info["text"])
            for k, chunk in enumerate(page_chunks):
                chunk_id = f"aaoifi_p{page_info['page']}_c{k}"
                all_chunks.append({
                    "id": chunk_id,
                    "text": chunk,
                    "source": "AAOIFI_Standard_21",
                    "page": page_info["page"],
                    "chunk": k,
                })
        total_chunks = len(all_chunks)
        print(f"  Created {total_chunks} chunks.")

        if total_chunks == 0:
            print("WARNING: No chunks created. PDF may be empty or image-only.")
            log_ingestion_run(engine, "aaoifi_pdf", pdf_path, 0, "success",
                              "No text chunks extracted from PDF.")
            return

        # 3. Generate embeddings
        print("Generating embeddings...")
        chunk_texts = [c["text"] for c in all_chunks]
        embeddings = generate_embeddings(chunk_texts)

        # 4. Build vectors and upsert to pgvector
        print("Upserting to pgvector...")
        vectors = []
        for chunk_data, embedding in zip(all_chunks, embeddings):
            vectors.append({
                "id": chunk_data["id"],
                "embedding": str(embedding),  # pgvector accepts string representation
                "source": chunk_data["source"],
                "page": chunk_data["page"],
                "chunk": chunk_data["chunk"],
                "text_content": chunk_data["text"][:2000],  # Truncate for storage
            })
        upsert_to_pgvector(engine, vectors)

        # 5. Log success
        print(f"\nIngestion complete: {total_chunks} chunks upserted.")
        log_ingestion_run(engine, "aaoifi_pdf", pdf_path, total_chunks, "success")

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}"
        print(f"\nERROR: Ingestion failed: {error_msg}")
        log_ingestion_run(engine, "aaoifi_pdf", pdf_path, total_chunks, "failed", error_msg)
        sys.exit(1)


if __name__ == "__main__":
    main()
