CREATE DATABASE db_sre;                                                                                                                                                                                                                    
CREATE DATABASE db_screener;                                                                                                                                                                                                               
CREATE DATABASE db_reeftracker;                                                                                                                                                                                                            
CREATE DATABASE db_e2ee_messenger;

-- Enable pgvector extension and create knowledge tables in db_sre
\c db_sre;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS knowledge_ingestion_runs (
    id SERIAL PRIMARY KEY,
    source_type VARCHAR(50) NOT NULL,
    source_path TEXT NOT NULL,
    chunks_upserted INTEGER NOT NULL,
    run_at TIMESTAMP DEFAULT NOW(),
    status VARCHAR(20) NOT NULL,
    error_message TEXT
);

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

-- Create an HNSW index for cosine similarity search
CREATE INDEX IF NOT EXISTS idx_knowledge_vectors_embedding
    ON knowledge_vectors
    USING hnsw (embedding vector_cosine_ops);