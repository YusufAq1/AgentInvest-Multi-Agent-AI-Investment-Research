"""Evaluation harness: retrieval, citation, and consistency evals.

Separate from tests/ (CLAUDE.md §12): these are non-pass/fail-per-commit
measurements (recall@k, faithfulness, run-to-run variance) run against
real indexed/generated data, not deterministic assertions run on every
commit.
"""
