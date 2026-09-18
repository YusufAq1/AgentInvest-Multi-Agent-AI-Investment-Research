"""Tests for backend.agents.financial.FinancialAgent.

No real network/API calls: XBRLClient is mocked entirely, and ClaudeClient
wraps a mocked Anthropic SDK client (matching tests/test_llm.py's
discipline) so the real forced-tool-use + retry mechanics run for real
against fake SDK responses.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from backend.agents.financial import FinancialAgent
from backend.core.llm import ClaudeClient
from backend.data.models import DataUnavailable, XBRLCompanyFacts, XBRLFact
from backend.data.xbrl import XBRLClient
from backend.evidence.store import EvidenceStore

from tests.conftest import make_settings

AS_OF = date(2024, 6, 30)

_REVENUE_FACT = XBRLFact(
    concept="Revenues",
    taxonomy="us-gaap",
    unit="USD",
    value=1_000_000,
    period_start=date(2023, 1, 1),
    period_end=date(2023, 12, 31),
    fiscal_year=2023,
    fiscal_period="FY",
    form="10-K",
    filed=date(2024, 2, 1),
    accession_number="acc-1",
)
_RAW_PAYLOAD = {
    "facts": {
        "us-gaap": {
            "Revenues": {
                "units": {
                    "USD": [
                        {
                            "start": "2023-01-01",
                            "end": "2023-12-31",
                            "val": 1_000_000,
                            "accn": "acc-1",
                            "fy": 2023,
                            "fp": "FY",
                            "form": "10-K",
                            "filed": "2024-02-01",
                        }
                    ]
                }
            }
        }
    }
}


def _mock_xbrl_client(result: XBRLCompanyFacts | DataUnavailable) -> MagicMock:
    mock = MagicMock(spec=XBRLClient)
    mock.get_company_facts_with_raw = AsyncMock(return_value=result)
    return mock


def _claim_batch_response(
    evidence_ids: list[str], *, tool_use_id: str = "toolu_1", claim_type: str = "evidence"
) -> SimpleNamespace:
    tool_input = {
        "claims": [
            {
                "id": str(uuid4()),
                "agent": "financial",
                "statement": "Revenue was $1,000,000 for FY2023",
                "evidence_ids": evidence_ids,
                "claim_type": claim_type,
                "materiality": "high",
            }
        ]
    }
    content = [
        SimpleNamespace(type="tool_use", id=tool_use_id, name="emit_claims", input=tool_input)
    ]
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    return SimpleNamespace(content=content, usage=usage, stop_reason="tool_use")


async def test_run_returns_empty_when_data_unavailable() -> None:
    unavailable = DataUnavailable(
        source="xbrl",
        identifier="AAPL",
        as_of=AS_OF,
        reason="no data",
        attempted_at=datetime.now(UTC),
    )
    mock_xbrl = _mock_xbrl_client(unavailable)
    mock_sdk_client = MagicMock()
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = FinancialAgent(mock_xbrl, claude, store, make_settings())

    claims = await agent.run("AAPL", AS_OF)

    assert claims == []
    mock_sdk_client.messages.create.assert_not_called()


@patch("backend.agents.financial.uuid4")
async def test_run_produces_claims_referencing_real_evidence(mock_uuid4: MagicMock) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id

    mock_xbrl = _mock_xbrl_client(XBRLCompanyFacts(facts=[_REVENUE_FACT], raw=_RAW_PAYLOAD))
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _claim_batch_response([str(fixed_evidence_id)])
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = FinancialAgent(mock_xbrl, claude, store, make_settings())

    claims = await agent.run("AAPL", AS_OF)

    assert len(claims) == 1
    assert claims[0].evidence_ids == [fixed_evidence_id]
    assert store.claims() == claims
    assert mock_sdk_client.messages.create.call_count == 1


@patch("backend.agents.financial.uuid4")
async def test_run_retries_once_when_claim_cites_unknown_evidence(mock_uuid4: MagicMock) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id
    hallucinated_id = str(uuid4())

    mock_xbrl = _mock_xbrl_client(XBRLCompanyFacts(facts=[_REVENUE_FACT], raw=_RAW_PAYLOAD))
    mock_sdk_client = MagicMock()
    bad_response = _claim_batch_response([hallucinated_id], tool_use_id="toolu_bad")
    good_response = _claim_batch_response([str(fixed_evidence_id)], tool_use_id="toolu_good")
    mock_sdk_client.messages.create.side_effect = [bad_response, good_response]
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = FinancialAgent(mock_xbrl, claude, store, make_settings())

    claims = await agent.run("AAPL", AS_OF)

    assert len(claims) == 1
    assert claims[0].evidence_ids == [fixed_evidence_id]
    assert mock_sdk_client.messages.create.call_count == 2
    assert store.dropped_claims() == []  # the retry succeeded — nothing was dropped


@patch("backend.agents.financial.uuid4")
async def test_run_drops_claims_when_both_attempts_cite_unknown_evidence(
    mock_uuid4: MagicMock,
) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id
    hallucinated_id_1 = str(uuid4())
    hallucinated_id_2 = str(uuid4())

    mock_xbrl = _mock_xbrl_client(XBRLCompanyFacts(facts=[_REVENUE_FACT], raw=_RAW_PAYLOAD))
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = [
        _claim_batch_response([hallucinated_id_1], tool_use_id="toolu_bad1"),
        _claim_batch_response([hallucinated_id_2], tool_use_id="toolu_bad2"),
    ]
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = FinancialAgent(mock_xbrl, claude, store, make_settings())

    claims = await agent.run("AAPL", AS_OF)

    assert claims == []
    assert store.claims() == []
    dropped = store.dropped_claims()
    assert len(dropped) == 1
    assert dropped[0].agent == "financial"
    assert dropped[0].reason  # non-empty — a real reason was recorded


def _duration_fact(concept: str, value: float, year: int) -> XBRLFact:
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
        accession_number=f"acc-{year}",
    )


def _duration_entry(value: float, year: int) -> dict[str, object]:
    return {
        "start": date(year, 1, 1).isoformat(),
        "end": date(year, 12, 31).isoformat(),
        "val": value,
        "accn": f"acc-{year}",
        "fy": year,
        "fp": "FY",
        "form": "10-K",
        "filed": date(year + 1, 2, 1).isoformat(),
    }


def _instant_fact(concept: str, value: float, year: int) -> XBRLFact:
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
        accession_number=f"acc-{year}",
    )


def _instant_entry(value: float, year: int) -> dict[str, object]:
    return {
        "end": date(year, 12, 31).isoformat(),
        "val": value,
        "accn": f"acc-{year}",
        "fy": year,
        "fp": "FY",
        "form": "10-K",
        "filed": date(year + 1, 2, 1).isoformat(),
    }


async def test_run_does_not_turn_entire_history_into_evidence() -> None:
    # Regression test: a real run against AAPL once produced 78k+ input
    # tokens because every historical XBRL fact matching the concept
    # aliases became Evidence. Fifteen years of revenue history here must
    # NOT all become evidence — only the current + prior-year anchors, plus
    # whatever's matched to the current period for the other ratios.
    years = range(2009, 2024)  # 15 years — the anchor is 2023, prior is 2022
    revenue_facts = [_duration_fact("Revenues", 1_000_000 + year, year) for year in years]
    revenue_entries = [_duration_entry(1_000_000 + year, year) for year in years]

    cogs_fact = _duration_fact("CostOfRevenue", 600_000, 2023)
    net_income_fact = _duration_fact("NetIncomeLoss", 150_000, 2023)
    ca_fact = _instant_fact("AssetsCurrent", 500_000, 2023)
    cl_fact = _instant_fact("LiabilitiesCurrent", 250_000, 2023)

    all_facts = [*revenue_facts, cogs_fact, net_income_fact, ca_fact, cl_fact]
    raw = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": revenue_entries}},
                "CostOfRevenue": {"units": {"USD": [_duration_entry(600_000, 2023)]}},
                "NetIncomeLoss": {"units": {"USD": [_duration_entry(150_000, 2023)]}},
                "AssetsCurrent": {"units": {"USD": [_instant_entry(500_000, 2023)]}},
                "LiabilitiesCurrent": {"units": {"USD": [_instant_entry(250_000, 2023)]}},
            }
        }
    }

    mock_xbrl = _mock_xbrl_client(XBRLCompanyFacts(facts=all_facts, raw=raw))
    mock_sdk_client = MagicMock()
    # Claim content doesn't matter for this test — only the evidence count.
    mock_sdk_client.messages.create.return_value = _claim_batch_response(
        [], claim_type="assumption"
    )
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = FinancialAgent(mock_xbrl, claude, store, make_settings())

    await agent.run("AAPL", AS_OF)

    # revenue(current) + revenue(prior) + cogs + net_income + ca + cl = 6,
    # regardless of 15 years of revenue history being available.
    assert len(store.all_evidence(source_type="xbrl_fact")) == 6
    # gross_margin, net_margin, yoy_revenue_growth, current_ratio = 4.
    assert len(store.all_evidence(source_type="computed")) == 4
    assert mock_sdk_client.messages.create.call_count == 1
