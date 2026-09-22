"""RAG over SEC filings: section-aware chunking, local BGE-M3 embeddings,
hybrid (pgvector HNSW + Postgres full-text) retrieval fused with
Reciprocal Rank Fusion. See CLAUDE.md §15 Phase 3 and ADR-0014.
"""
