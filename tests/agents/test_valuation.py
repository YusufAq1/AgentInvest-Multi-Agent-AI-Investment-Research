"""Tests for the Valuation Agent and its evidence (Phase 6, Increment 6c).

Uses the hand-checkable synthetic company TICK from test_valuation_inputs.
The key property: every evidence row the agent stores passes the same
citation-validity sweep CI runs (backend/evidence/validation.py). XBRL
quotes are found verbatim in the raw payload, market rows match their
canonical rendering, and every computed number, implied growth included,
recomputes from the rows it cites. Tampering with any number breaks that.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from backend.agents.valuation import ValuationAgent, ValuationUnavailableError
from backend.agents.valuation_compute import compute_valuation
from backend.agents.valuation_evidence import ValuationEvidence, build_valuation_evidence
from backend.agents.valuation_inputs import ValuationInputs
from backend.core.llm import ClaudeClient
from backend.evidence.models import Evidence
from backend.evidence.store import EvidenceStore
from backend.evidence.validation import CitationFailure, validate_all_evidence

from tests.agents.test_valuation_inputs import (
    AS_OF,
    TICKER,
    assemble,
    assembler_for,
    company_facts,
)
from tests.conftest import assert_sdk_accepts_every_call, make_settings


def _validate(rows: list[Evidence], inputs: ValuationInputs) -> list[CitationFailure]:
    by_id = {row.id: row for row in rows}

    def resolve(ids: Any) -> list[Evidence]:
        return [by_id[UUID(str(i))] for i in ids]

    raws = {row.source_ref: inputs.xbrl_raw for row in rows if row.source_type == "xbrl_fact"}
    return validate_all_evidence(rows, xbrl_raw_payloads=raws, resolve=resolve)


async def _evidence(**kwargs: Any) -> tuple[ValuationEvidence, ValuationInputs]:
    inputs = await assemble(company_facts(**kwargs))
    result = compute_valuation(inputs, make_settings())
    return build_valuation_evidence(result, uuid4(), make_settings()), inputs


async def test_every_valuation_row_passes_the_citation_sweep() -> None:
    evidence, inputs = await _evidence()

    assert _validate(evidence.rows, inputs) == []
    kinds = {row.source_type for row in evidence.rows}
    assert kinds == {"xbrl_fact", "price_series", "macro", "computed"}
    assert evidence.headline["implied_growth"].location["ratio_name"] == "implied_revenue_growth"


async def test_no_valuation_row_postdates_as_of() -> None:
    evidence, _ = await _evidence()
    assert all(row.published_at <= AS_OF for row in evidence.rows)


async def test_tampering_with_a_computed_number_fails_recompute() -> None:
    """Change the stored operating margin. Its own recompute fails, and so
    does the implied growth that consumed it, because that row recomputes
    from the margin row's (now wrong) value."""
    evidence, inputs = await _evidence()
    margin = evidence.headline["operating_margin"]
    tampered = margin.model_copy(update={"location": {**margin.location, "value": 0.35}})
    rows = [tampered if row.id == margin.id else row for row in evidence.rows]

    failed = {f.evidence.location.get("ratio_name") for f in _validate(rows, inputs)}

    assert "average_operating_margin" in failed
    assert "implied_revenue_growth" in failed


async def test_tampering_with_a_market_datum_fails() -> None:
    evidence, inputs = await _evidence()
    price = evidence.headline["price"]
    tampered = price.model_copy(update={"location": {**price.location, "value": 99.0}})
    rows = [tampered if row.id == price.id else row for row in evidence.rows]

    failed_ids = {f.evidence.id for f in _validate(rows, inputs)}

    assert price.id in failed_ids  # quote no longer matches its value
    assert evidence.headline["market_cap"].id in failed_ids  # recomputes from the price


async def test_assumed_inputs_become_recomputable_labelled_rows() -> None:
    shrinking: dict[int, float] = {
        2018: 1500,
        2019: 1450,
        2020: 1400,
        2021: 1300,
        2022: 1250,
        2023: 1200,
    }
    evidence, inputs = await _evidence(revenues=shrinking, include_interest=False)

    names = {row.location.get("ratio_name") for row in evidence.rows}
    assert {"assumed_incremental_investment_rate", "assumed_cost_of_debt"} <= names
    assert _validate(evidence.rows, inputs) == []


def _sdk_citing(evidence_id: str) -> MagicMock:
    sdk = MagicMock()
    sdk.messages.create.return_value = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="emit_claims",
                input={
                    "claims": [
                        {
                            "id": str(uuid4()),
                            "agent": "valuation",
                            "statement": "The price implies faster growth than recent history.",
                            "evidence_ids": [evidence_id],
                            "claim_type": "inference",
                            "materiality": "high",
                        }
                    ]
                },
            )
        ],
        usage=SimpleNamespace(
            input_tokens=900,
            output_tokens=120,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        stop_reason="tool_use",
    )
    return sdk


async def test_agent_stores_evidence_and_a_cited_claim() -> None:
    settings = make_settings()
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    holder: dict[str, MagicMock] = {}

    class _Claude(ClaudeClient):
        def call_structured(self, **kwargs: Any) -> Any:
            # Cite the real implied-growth row the agent just stored.
            implied = next(
                row
                for row in store.all_evidence(source_type="computed")
                if row.location["ratio_name"] == "implied_revenue_growth"
            )
            self._client = holder.setdefault("sdk", _sdk_citing(str(implied.id)))
            return super().call_structured(**kwargs)

    agent = ValuationAgent(
        assembler_for(company_facts()), _Claude(settings, client=MagicMock()), store, settings
    )
    claims = await agent.run(TICKER, AS_OF)

    assert len(claims) == 1 and store.claims() == claims
    assert len(store.all_evidence()) > 20
    assert_sdk_accepts_every_call(holder["sdk"])
    system = holder["sdk"].messages.create.call_args.kwargs["system"]
    assert "Revenue growth per year the market price implies" in system
    assert "growth creates value" in system
    assert "{evidence_context}" not in system and "{assumptions}" not in system


async def test_unavailable_inputs_raise_with_every_reason() -> None:
    """Raised, so the graph's bulkhead records a FAILED outcome listing
    what's missing, rather than a silent empty success."""
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = ValuationAgent(
        assembler_for(company_facts(include_debt=False)),
        ClaudeClient(make_settings(), client=MagicMock()),
        store,
        make_settings(),
    )
    with pytest.raises(ValuationUnavailableError, match="long-term debt"):
        await agent.run(TICKER, AS_OF)
    assert store.all_evidence() == []
