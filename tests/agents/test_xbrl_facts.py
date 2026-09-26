"""Tests for backend.agents.xbrl_facts — the shared XBRL fact-selection
helpers extracted from backend/agents/financial.py (see ADR-0016).

Previously only exercised indirectly through tests/agents/test_financial.py;
these are direct, focused tests now that this is shared, reusable code.
"""

from datetime import date

from backend.agents.xbrl_facts import (
    fact_key,
    select_anchor,
    select_matching,
    select_prior_year,
)
from backend.data.models import XBRLFact


def _duration_fact(concept: str, value: float, year: int, accession: str | None = None) -> XBRLFact:
    return XBRLFact(
        concept=concept,
        taxonomy="us-gaap",
        unit="USD",
        value=value,
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        fiscal_year=year,
        fiscal_period="FY",
        form="10-K",
        filed=date(year + 1, 2, 1),
        accession_number=accession or f"acc-{year}",
    )


def _instant_fact(concept: str, value: float, year: int, accession: str | None = None) -> XBRLFact:
    return XBRLFact(
        concept=concept,
        taxonomy="us-gaap",
        unit="USD",
        value=value,
        period_start=None,
        period_end=date(year, 12, 31),
        fiscal_year=year,
        fiscal_period="FY",
        form="10-K",
        filed=date(year + 1, 2, 1),
        accession_number=accession or f"acc-{year}",
    )


def test_fact_key_distinguishes_by_concept_accession_and_period() -> None:
    a = _duration_fact("Revenues", 100, 2023)
    b = _duration_fact("Revenues", 200, 2022)
    assert fact_key(a) != fact_key(b)
    assert fact_key(a) == fact_key(a)


def test_select_anchor_picks_most_recent_duration_fact() -> None:
    facts = [_duration_fact("Revenues", 100, 2021), _duration_fact("Revenues", 200, 2023)]

    anchor = select_anchor(facts, instant=False)

    assert anchor is not None
    assert anchor.value == 200


def test_select_anchor_picks_most_recent_instant_fact() -> None:
    facts = [_instant_fact("AssetsCurrent", 100, 2021), _instant_fact("AssetsCurrent", 300, 2023)]

    anchor = select_anchor(facts, instant=True)

    assert anchor is not None
    assert anchor.value == 300


def test_select_anchor_returns_none_when_no_matching_shape() -> None:
    facts = [_duration_fact("Revenues", 100, 2023)]

    assert select_anchor(facts, instant=True) is None


def test_select_matching_requires_same_accession_and_period() -> None:
    anchor = _duration_fact("Revenues", 100, 2023, accession="acc-x")
    matching_cogs = _duration_fact("CostOfRevenue", 60, 2023, accession="acc-x")
    mismatched_period_cogs = _duration_fact("CostOfRevenue", 999, 2022, accession="acc-x")

    result = select_matching([matching_cogs, mismatched_period_cogs], ("CostOfRevenue",), anchor)

    assert result is not None
    assert result.value == 60


def test_select_matching_returns_none_without_a_match() -> None:
    anchor = _duration_fact("Revenues", 100, 2023)

    assert select_matching([], ("CostOfRevenue",), anchor) is None


def test_select_prior_year_finds_fact_near_one_year_earlier() -> None:
    anchor = _duration_fact("Revenues", 200, 2023)
    prior = _duration_fact("Revenues", 150, 2022)

    result = select_prior_year([anchor, prior], anchor)

    assert result is not None
    assert result.value == 150


def test_select_prior_year_returns_none_outside_tolerance() -> None:
    anchor = _duration_fact("Revenues", 200, 2023)
    # period_end far more than PRIOR_YEAR_TOLERANCE_DAYS away from one year prior.
    too_early = XBRLFact(
        concept="Revenues",
        taxonomy="us-gaap",
        unit="USD",
        value=50,
        period_start=date(2020, 1, 1),
        period_end=date(2020, 12, 31),
        fiscal_year=2020,
        fiscal_period="FY",
        form="10-K",
        filed=date(2021, 2, 1),
        accession_number="acc-2020",
    )

    assert select_prior_year([anchor, too_early], anchor) is None


def test_select_prior_year_returns_none_without_candidates() -> None:
    anchor = _duration_fact("Revenues", 200, 2023)

    assert select_prior_year([anchor], anchor) is None
