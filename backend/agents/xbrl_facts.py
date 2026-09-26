"""Shared XBRL fact-selection helpers, used by any agent that reads XBRL
companyfacts and needs to resolve filer-specific concept tags to a
consistent set of inputs (revenue, cogs, net income, ...).

WHY this module exists separately from backend/calc/: XBRL tagging varies
across filers (e.g. "Revenues" vs "SalesRevenueNet") — ratios.py stays
agnostic to tagging conventions and only takes plain floats; resolving
"which concept means revenue for this filer" is agent-layer work. It was
originally private to backend/agents/financial.py (Phase 2); extracted
here once the Competitive Agent (Phase 4) needed the identical selection
logic across multiple companies, not just one (see ADR-0016) — CLAUDE.md
§17's "agents that all do the same thing" applies to shared plumbing like
this, not only to whole agents.

Known, documented gap: a filer using a concept tag outside CONCEPT_ALIASES
has that input simply skipped, not fabricated (C6).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from backend.data.models import XBRLFact

CONCEPT_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "revenue": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "SalesRevenueNet",
    ),
    "cogs": ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"),
    "net_income": ("NetIncomeLoss",),
    "current_assets": ("AssetsCurrent",),
    "current_liabilities": ("LiabilitiesCurrent",),
}
ALL_ALIASES: Final[tuple[str, ...]] = tuple(
    alias for aliases in CONCEPT_ALIASES.values() for alias in aliases
)

# Tolerance for matching "the same point in the fiscal calendar, one year
# earlier" when selecting a prior-year comparison fact for YoY growth.
PRIOR_YEAR_TOLERANCE_DAYS: Final[int] = 45

FactKey = tuple[str, str, str, str | None]


def fact_key(fact: XBRLFact) -> FactKey:
    return (
        fact.concept,
        fact.accession_number,
        fact.period_end.isoformat(),
        fact.period_start.isoformat() if fact.period_start else None,
    )


def select_anchor(facts: list[XBRLFact], *, instant: bool) -> XBRLFact | None:
    """Picks the most-recent (by period_end, then filed) fact of the given
    shape (instant = balance-sheet-style point-in-time; duration = a
    revenue/income-style figure over a period).

    Known simplification: XBRL companyfacts contains many overlapping
    periods (a 10-K's annual figure, a 10-Q's quarterly and
    year-to-date cumulative figures for the same concept). "Most recent
    period_end" is a reasonable, documented heuristic for a first version,
    not a rigorous fiscal-period-aware selector.
    """
    candidates = [f for f in facts if (f.period_start is None) == instant]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.period_end, f.filed))


def select_matching(
    facts: list[XBRLFact], aliases: tuple[str, ...], anchor: XBRLFact
) -> XBRLFact | None:
    """Finds a fact for one of `aliases` from the SAME accession and
    period as `anchor` — so e.g. gross_margin's revenue and cogs come from
    the same filing's same reporting period, not mismatched periods."""
    for fact in facts:
        if (
            fact.concept in aliases
            and fact.accession_number == anchor.accession_number
            and fact.period_end == anchor.period_end
            and fact.period_start == anchor.period_start
        ):
            return fact
    return None


def select_prior_year(facts: list[XBRLFact], anchor: XBRLFact) -> XBRLFact | None:
    """Finds a duration fact for the same concept whose period_end lands
    close to one year before `anchor`'s, for YoY growth."""
    candidates = [
        fact
        for fact in facts
        if fact.period_start is not None and fact.period_end < anchor.period_end
    ]
    if not candidates:
        return None
    target = anchor.period_end - timedelta(days=365)
    best = min(candidates, key=lambda f: abs((f.period_end - target).days))
    if abs((best.period_end - target).days) > PRIOR_YEAR_TOLERANCE_DAYS:
        return None
    return best
