"""Tests for evaluation.retrieval_eval's pure scoring logic and dataset
loading — no DB, no real hybrid_search call (that needs a real indexed
corpus, which is what the script itself is for, not something to fake in
a unit test).
"""

import math
from datetime import date
from pathlib import Path

from evaluation.retrieval_eval import (
    QuestionResult,
    RetrievalEvalQuestion,
    _score,
    load_dataset,
)

_QUESTION = RetrievalEvalQuestion(
    question="What are the risk factors?",
    ticker="AAPL",
    as_of=date(2024, 6, 30),
    accession="ACC-1",
    item="1A",
)


def test_score_all_hits_at_rank_one_gives_perfect_recall_and_ndcg() -> None:
    results = [QuestionResult(question=_QUESTION, hit_rank=1) for _ in range(3)]

    report = _score(results, top_k=5)

    assert report.recall_at_k == 1.0
    assert report.ndcg_at_k == 1.0
    assert report.misses == []


def test_score_no_hits_gives_zero_recall_and_ndcg() -> None:
    results = [QuestionResult(question=_QUESTION, hit_rank=None) for _ in range(3)]

    report = _score(results, top_k=5)

    assert report.recall_at_k == 0.0
    assert report.ndcg_at_k == 0.0
    assert len(report.misses) == 3


def test_score_partial_hits_and_lower_rank_reduces_ndcg_but_not_recall_denominator() -> None:
    hit_at_rank_1 = QuestionResult(question=_QUESTION, hit_rank=1)
    hit_at_rank_3 = QuestionResult(question=_QUESTION, hit_rank=3)
    miss = QuestionResult(question=_QUESTION, hit_rank=None)

    report = _score([hit_at_rank_1, hit_at_rank_3, miss], top_k=5)

    assert report.recall_at_k == 2 / 3
    expected_ndcg = (1.0 + 1.0 / math.log2(4)) / 3
    assert report.ndcg_at_k == expected_ndcg
    assert len(report.misses) == 1


def test_score_empty_input_does_not_divide_by_zero() -> None:
    report = _score([], top_k=5)

    assert report.recall_at_k == 0.0
    assert report.ndcg_at_k == 0.0
    assert report.misses == []


def test_load_dataset_missing_file_returns_empty_list(tmp_path: Path) -> None:
    assert load_dataset(tmp_path / "does-not-exist.jsonl") == []


def test_load_dataset_skips_blank_lines_and_comments(tmp_path: Path) -> None:
    dataset_path = tmp_path / "eval.jsonl"
    dataset_path.write_text(
        "# a leading comment\n"
        "\n"
        '{"question": "q1", "ticker": "AAPL", "as_of": "2024-06-30", '
        '"accession": "ACC-1", "item": "1A"}\n',
        encoding="utf-8",
    )

    questions = load_dataset(dataset_path)

    assert len(questions) == 1
    assert questions[0].question == "q1"
    assert questions[0].item == "1A"


def test_real_dataset_file_parses_without_error() -> None:
    # Regression check for the actual committed dataset — proves the
    # starter set (evaluation/datasets/retrieval_eval.jsonl) is valid
    # JSONL matching RetrievalEvalQuestion's schema, not just an example.
    questions = load_dataset()

    assert len(questions) >= 1
    for question in questions:
        assert question.ticker
        assert question.item
