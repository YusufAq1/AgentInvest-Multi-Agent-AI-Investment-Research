"""Local BGE-M3 dense embeddings.

WHY BGE-M3 run locally instead of a paid embeddings API: Anthropic has no
embeddings endpoint, and any paid embeddings API would violate CLAUDE.md
C1 (all data sources must be free). BGE-M3 is a free, CPU-runnable model
that produces dense vectors from ordinary sentence-transformers usage.

WHY sentence-transformers instead of FlagEmbedding (BAAI's own reference
implementation): FlagEmbedding additionally gives sparse and ColBERT
vectors, but this project's hybrid retrieval design (see
backend/rag/retrieval.py and ADR-0014) only ever fuses dense vectors with
Postgres full-text search — the sparse/ColBERT outputs have no consumer
here, so the lighter, more broadly-maintained library is sufficient.

WHY a lazy, double-checked-locking singleton, not eager module-level
instantiation: importing `backend.rag.*` happens transitively in every
test that touches chunking (which needs `get_tokenizer()`) or embeddings.
An eager `SentenceTransformer(...)` at import time would force a ~2GB
model download and a multi-second load on every test run, including pure
-logic tests (RRF fusion math, offset arithmetic) that never touch a real
model. Lazy init means only code paths that actually call `embed_texts`/
`get_tokenizer` trigger the load, and both are trivially mockable in unit
tests by monkeypatching the module-level `_model` variable directly. The
lock guards a real (if narrow) race: `backend/rag/indexing.py` runs
concurrently via `asyncio.gather` (CLAUDE.md §16), and two coroutines
could both observe `_model is None` before either finishes constructing it
— the synchronous part of `_get_model()` runs on the event loop thread
before any `await`, so an ordinary unlocked check-then-set is a real,
not theoretical, double-construction risk.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from sentence_transformers import SentenceTransformer

if TYPE_CHECKING:
    from transformers import PreTrainedTokenizerFast

# BGE-M3's dense output dimension — a property of the model, not a tunable
# knob, so it's defined once here (next to the model choice it derives
# from) and imported everywhere else that needs it (backend/db/models.py's
# `Vector(EMBEDDING_DIM)` column, eval scoring, etc.) rather than repeated
# as a bare literal in multiple files.
EMBEDDING_DIM: Final[int] = 1024

_MODEL_NAME: Final[str] = "BAAI/bge-m3"

_model: SentenceTransformer | None = None
_model_lock = threading.Lock()


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:  # re-check inside the lock
                _model = SentenceTransformer(_MODEL_NAME, device="cpu")
    return _model


def get_tokenizer() -> PreTrainedTokenizerFast:
    """The HuggingFace fast tokenizer BGE-M3 uses — backend/rag/chunking.py
    uses this (not a second, separate tokenizer dependency) to sub-chunk
    long sections by token count, since its `offset_mapping` output lets a
    sub-chunk's exact char span be recovered without a second substring
    search."""
    return _get_model().tokenizer  # type: ignore[no-any-return]


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    """Returns one 1024-dim dense vector per input text, in input order.

    Runs the synchronous sentence-transformers `encode()` call in a thread
    via `asyncio.to_thread`, matching every other sync-library wrapper in
    this codebase (backend/data/edgar.py, backend/data/prices.py) — the
    model's `encode()` has no async counterpart.
    """
    if not texts:
        return []
    vectors = await asyncio.to_thread(_encode_sync, list(texts))
    return [vector.tolist() for vector in vectors]


def _encode_sync(texts: list[str]) -> Any:
    model = _get_model()
    # normalize_embeddings=True: pgvector's cosine-distance operator
    # (`vector_cosine_ops`, used by document_chunks' HNSW index — see the
    # Phase 3 migration and ADR-0014) is what BGE-M3 is trained for;
    # normalizing keeps stored vectors consistent with that.
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
