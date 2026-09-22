"""Phase 3 exit criterion: recall@5 (and NDCG@5) on a hand-labelled
retrieval question set, measured against `backend.rag.retrieval.hybrid_search`.

Run with (after indexing the filings the dataset's questions reference —
see scripts/rag_demo.py):
    uv run python -m evaluation.retrieval_eval

CLAUDE.md §12: "Hand-label ~30 questions with their correct filing section
... Measure recall@k and NDCG@k. An afternoon of labelling buys a real
metric." This script is the scoring half of that — the labelling itself
(evaluation/datasets/retrieval_eval.jsonl) is manual work done separately,
against real filings, not something this script derives.

WHY this is a human-run script, not a CI-gating test: it needs a real
indexed corpus (real filings, real embeddings) to produce a meaningful
number — recall@5 against synthetic seed data in CI would measure nothing
real. tests/rag/test_retrieval.py already covers the retrieval *mechanics*
against real Postgres with synthetic, deterministic data; this script
measures whether BGE-M3 + hybrid search actually finds the right answer in
real filings, which only makes sense against real ones.
"""

import asyncio
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from backend.core.config import Settings
from backend.db.session import make_engine, make_session_factory
from backend.rag.retrieval import RetrievedChunk, hybrid_search
from pydantic import BaseModel

_DEFAULT_DATASET = Path(__file__).parent / "datasets" / "retrieval_eval.jsonl"


class RetrievalEvalQuestion(BaseModel):
    """One labelled row: a question and the (accession, item) that answers
    it. `as_of` matters because retrieval is point-in-time (C3) — a
    question is evaluated against a specific index snapshot, not "whatever
    happens to be indexed right now."
    """

    question: str
    ticker: str
    as_of: date
    accession: str
    item: str
    notes: str = ""


@dataclass
class QuestionResult:
    question: RetrievalEvalQuestion
    hit_rank: int | None  # 1-indexed rank of the first matching result, None if no hit


class RetrievalEvalReport(BaseModel):
    top_k: int
    total_questions: int
    recall_at_k: float
    ndcg_at_k: float
    misses: list[str]  # questions with no hit, for manual follow-up


def load_dataset(path: Path = _DEFAULT_DATASET) -> list[RetrievalEvalQuestion]:
    if not path.exists():
        return []
    questions = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            questions.append(RetrievalEvalQuestion.model_validate(json.loads(line)))
    return questions


def _iter_results(
    question: RetrievalEvalQuestion, results: list[RetrievedChunk]
) -> Iterator[tuple[int, bool]]:
    for rank, result in enumerate(results, start=1):
        yield rank, (result.accession == question.accession and result.item == question.item)


async def run_retrieval_eval(
    questions: list[RetrievalEvalQuestion],
    *,
    settings: Settings,
) -> RetrievalEvalReport:
    engine = make_engine(settings)
    session_factory = make_session_factory(engine)

    question_results: list[QuestionResult] = []
    for question in questions:
        results = await hybrid_search(
            query=question.question,
            ticker=question.ticker,
            as_of=question.as_of,
            session_factory=session_factory,
            top_k=settings.rag_retrieval_top_k,
            candidate_k=settings.rag_retrieval_candidate_k,
            rrf_k=settings.rag_rrf_k,
        )
        hit_rank = next((rank for rank, is_hit in _iter_results(question, results) if is_hit), None)
        question_results.append(QuestionResult(question=question, hit_rank=hit_rank))

    await engine.dispose()
    return _score(question_results, top_k=settings.rag_retrieval_top_k)


def _score(question_results: list[QuestionResult], *, top_k: int) -> RetrievalEvalReport:
    total = len(question_results)
    hits = [r for r in question_results if r.hit_rank is not None]

    recall_at_k = len(hits) / total if total else 0.0
    # NDCG@k with binary, single-label relevance reduces to
    # 1/log2(rank+1) for the first (only) relevant hit, 0 if there's none
    # — stated explicitly since this dataset has no graded relevance.
    hit_ranks = [r.hit_rank for r in hits if r.hit_rank is not None]
    ndcg_terms = [1.0 / math.log2(rank + 1) for rank in hit_ranks]
    ndcg_at_k = sum(ndcg_terms) / total if total else 0.0

    misses = [r.question.question for r in question_results if r.hit_rank is None]
    return RetrievalEvalReport(
        top_k=top_k,
        total_questions=total,
        recall_at_k=recall_at_k,
        ndcg_at_k=ndcg_at_k,
        misses=misses,
    )


def _print_report(report: RetrievalEvalReport) -> None:
    print(f"=== Retrieval eval: {report.total_questions} questions, top_k={report.top_k} ===")
    print(f"recall@{report.top_k}: {report.recall_at_k:.3f}")
    print(f"NDCG@{report.top_k}:   {report.ndcg_at_k:.3f}")
    if report.misses:
        print(f"\n--- {len(report.misses)} miss(es) ---")
        for question in report.misses:
            print(f"  - {question}")


async def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    questions = load_dataset()
    if not questions:
        print(
            f"No labelled questions found at {_DEFAULT_DATASET}. "
            "See CLAUDE.md §12 — hand-label ~30 questions there before running this."
        )
        return
    report = await run_retrieval_eval(questions, settings=settings)
    _print_report(report)


if __name__ == "__main__":
    asyncio.run(main())
