"""Initial schema: documents, document_chunks, pgvector + full-text indexes.

Revision ID: 0001
Revises:
Create Date: 2026-09-19

WHY the HNSW and GIN indexes are created in this same migration, right
after the tables, rather than deferred to a later one: CLAUDE.md §14 is
explicit that the HNSW index must be "built on an empty table" — there is
no data-loading step between table creation and index creation anywhere in
this project's Phase 3 flow, so splitting index creation into a second
migration would only add an artificial window where it could be forgotten.

WHY `vector_cosine_ops`, not `vector_l2_ops`: BGE-M3 (backend/rag/
embeddings.py) is trained for cosine similarity, and its embeddings are
stored normalized (`normalize_embeddings=True`) specifically to match —
using L2 distance here would be a silent mismatch with how the model was
trained. See ADR-0014.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from backend.rag.embeddings import EMBEDDING_DIM
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("accession", sa.String(), nullable=False),
        sa.Column("ticker", sa.String(), nullable=False),
        sa.Column("form", sa.String(), nullable=False),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_documents_accession", "documents", ["accession"], unique=True)
    op.create_index("ix_documents_ticker", "documents", ["ticker"])

    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("accession", sa.String(), nullable=False),
        sa.Column("item", sa.String(), nullable=True),
        sa.Column("filing_date", sa.Date(), nullable=False),
        sa.Column("fiscal_period", sa.String(), nullable=True),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_document_chunks_accession_chunk_index",
        "document_chunks",
        ["accession", "chunk_index"],
        unique=True,
    )
    op.create_index("ix_document_chunks_accession", "document_chunks", ["accession"])

    # Built on the empty table just created — CLAUDE.md §14.
    op.execute(
        "CREATE INDEX document_chunks_embedding_hnsw_idx ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    op.execute("CREATE INDEX document_chunks_tsv_gin_idx ON document_chunks USING gin (tsv)")


def downgrade() -> None:
    op.drop_table("document_chunks")
    op.drop_table("documents")
