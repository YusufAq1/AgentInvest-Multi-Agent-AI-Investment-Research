"""Tests for backend.rag.retrieval.fuse_rankings — pure RRF math, no DB.

Hand-computed expected orderings, per ADR-0014's formula:
score(d) = sum over each ranked list containing d of 1 / (rrf_k + rank).
"""

from uuid import uuid4

from backend.rag.retrieval import fuse_rankings

RRF_K = 60


def test_chunk_in_both_lists_outranks_chunk_in_one() -> None:
    a, b = uuid4(), uuid4()
    # `a` is rank 1 in both lists; `b` is rank 2 in only the dense list.
    fused = fuse_rankings([a, b], [a], RRF_K)

    assert fused[0][0] == a
    assert fused[1][0] == b
    expected_a = 1 / (RRF_K + 1) + 1 / (RRF_K + 1)
    expected_b = 1 / (RRF_K + 2)
    assert fused[0][1] == expected_a
    assert fused[1][1] == expected_b


def test_lower_rank_in_a_single_list_still_scores_lower() -> None:
    a, b = uuid4(), uuid4()
    fused = fuse_rankings([a, b], [], RRF_K)

    assert fused[0][0] == a
    assert fused[0][1] == 1 / (RRF_K + 1)
    assert fused[1][0] == b
    assert fused[1][1] == 1 / (RRF_K + 2)


def test_chunk_missing_from_both_lists_is_absent_from_result() -> None:
    a = uuid4()
    fused = fuse_rankings([a], [a], RRF_K)

    assert [chunk_id for chunk_id, _ in fused] == [a]


def test_empty_lists_produce_empty_fusion() -> None:
    assert fuse_rankings([], [], RRF_K) == []


def test_disjoint_lists_are_ordered_by_their_own_rank_only() -> None:
    a, b, c, d = uuid4(), uuid4(), uuid4(), uuid4()
    # Dense list: a (rank 1), b (rank 2). Full-text list: c (rank 1), d (rank 2).
    # a and c (both rank 1 in their own list) should score identically and
    # both outrank b and d (rank 2 in their own lists).
    fused = fuse_rankings([a, b], [c, d], RRF_K)
    scores = dict(fused)

    assert scores[a] == scores[c] == 1 / (RRF_K + 1)
    assert scores[b] == scores[d] == 1 / (RRF_K + 2)
    assert scores[a] > scores[b]
