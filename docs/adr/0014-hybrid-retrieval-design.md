# ADR-0014: Hybrid retrieval (pgvector HNSW + Postgres full-text) fused with Reciprocal Rank Fusion

## Status
Accepted — 2026-09-19

## Context
CLAUDE.md §4 specifies "pgvector HNSW + Postgres `tsvector` full-text,
fused with Reciprocal Rank Fusion" for retrieval, and this is the seed
topic of the spec's own ADR-0004 slot (§11.1) — not built until now
because Phase 3 is the first phase with anything to retrieve over. It gets
recorded as ADR-0014, not 0004, following this project's established
precedent (ADR-0008/0010/0011/0012 already show ADR numbers are assigned
by write order, not backfilled to match CLAUDE.md's seed list).

Dense embeddings alone have a well-known failure mode for this domain:
a query containing an exact ticker, dollar figure, or product name can
score worse by cosine similarity than a vaguer, topically-related passage,
because embedding models compress exact lexical detail into the same
vector space as paraphrase. Full-text search alone has the opposite
failure mode: a paraphrased query with no shared vocabulary with the
answer scores zero. Neither alone is sufficient; the question was how to
combine them.

## Decision
**Two independent ranked lists, fused by Reciprocal Rank Fusion (RRF),
not a single blended score.**

1. **Dense leg**: BGE-M3 embeddings (`backend/rag/embeddings.py`), stored
   normalized (`normalize_embeddings=True`), queried via pgvector's
   `vector_cosine_ops` HNSW index (`m=16, ef_construction=64`, built on
   the empty table per CLAUDE.md §14) using the `<=>` cosine-distance
   operator. Cosine, not L2 (`vector_l2_ops`), because BGE-M3 is trained
   for cosine similarity — L2 would be a silent mismatch with how the
   model was trained.
2. **Full-text leg**: Postgres `tsvector` (`to_tsvector('english', text)`,
   a *generated* column so it's always in sync with `text` with no
   application-level maintenance), queried via `plainto_tsquery` and
   ranked by `ts_rank`.
3. **Fusion**: `score(chunk) = sum over each ranked list containing it of
   1 / (rrf_k + rank)`, `rank` 1-indexed, `rrf_k = 60` — the original RRF
   paper's (Cormack et al., 2009) constant, adopted widely (e.g.
   Elasticsearch's hybrid search defaults to it) because it flattens the
   impact of rank 1 vs. rank 2 without needing a metric-specific
   calibration. RRF was chosen specifically **because** it only needs each
   list's *rank*, not its raw score — cosine distance and `ts_rank` are on
   incompatible scales with no principled shared unit, so any weighted
   blend of the raw scores would need an arbitrary calibration constant
   RRF avoids entirely.
4. **Implementation**: the two legs run as two separate SQL queries in one
   session (`backend/rag/retrieval.py`), and fusion happens in Python via
   a standalone `fuse_rankings()` function — not a single SQL-level
   UNION/CTE — specifically so the fusion math is unit-testable against
   hand-computed expected orderings without a database
   (`tests/rag/test_retrieval_fusion.py`).

## Alternatives considered
- **Dense embeddings only.** Rejected: fails on exact-term queries
  (tickers, dollar figures, product names) that full-text search catches
  trivially — this is the concrete gap hybrid search exists to close.
- **Full-text only.** Rejected: fails on paraphrased/semantic queries with
  no shared vocabulary — the entire reason embeddings are being used at
  all in this project's RAG design.
- **A single SQL query with a hand-blended score
  (`w1 * cosine_similarity + w2 * ts_rank`).** Rejected: cosine similarity
  and `ts_rank` have no shared, principled scale — any `w1`/`w2` would be
  an arbitrary tuning knob with no way to justify a specific value, which
  conflicts with CLAUDE.md §0's "no silent decisions" — RRF's rank-only
  fusion sidesteps needing that knob at all.
- **BGE-M3's own sparse/ColBERT output as the second leg, instead of
  Postgres full-text.** Rejected (see also `backend/rag/embeddings.py`'s
  module docstring and the choice of `sentence-transformers` over
  `FlagEmbedding`): Postgres `tsvector` is free, already available in the
  chosen database, and needs no second model inference pass per query —
  sparse/ColBERT vectors would duplicate what full-text search already
  gives for less engineering cost.

## Consequences
Easy: each leg is independently swappable — a future embedding model
change only touches the dense leg's column type/index, and a fusion
constant (`rrf_k`) change is a one-line config edit
(`Settings.rag_rrf_k`), not a code change. The eval harness
(`evaluation/retrieval_eval.py`) can attribute a hit to "found via dense,"
"found via full-text," or "found via both," which is a genuinely useful
debugging signal a single blended score would hide.

Hard: two queries per search instead of one, and `candidate_k` (each leg's
over-fetch before fusion) is a second tunable knob beyond `top_k` — both
now live in `Settings` (`rag_retrieval_candidate_k`, `rag_retrieval_top_k`)
rather than being hardcoded, per CLAUDE.md §16, but they're real
knobs whose defaults (20 and 5) haven't been tuned against real recall
data yet — revisit once `evaluation/retrieval_eval.py`'s recall@5 result is
in and there's a number to tune against.
