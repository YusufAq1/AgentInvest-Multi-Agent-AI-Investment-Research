"""Tests for backend.agents.competitive.CompetitiveAgent.

No real network calls: XBRLClient is mocked entirely, and ClaudeClient
wraps a mocked Anthropic SDK client (matching tests/test_llm.py's
discipline) so the real forced-tool-use + retry mechanics run for real
against fake SDK responses. A small fixture peer_map is injected via the
constructor instead of the real backend.agents.peer_map.PEER_MAP.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from backend.agents.competitive import CompetitiveAgent
from backend.core.llm import ClaudeClient
from backend.data.models import DataUnavailable, XBRLCompanyFacts, XBRLFact
from backend.data.xbrl import XBRLClient
from backend.evidence.store import EvidenceStore

from tests.conftest import make_settings

AS_OF = date(2024, 6, 30)
TICKER = "TICK"
PEER = "PEER1"
_FIXTURE_PEER_MAP: dict[str, tuple[str, ...]] = {TICKER: (PEER,)}


def _company_facts(value: float, accession: str) -> XBRLCompanyFacts:
    revenue_entry = {
        "start": "2023-01-01",
        "end": "2023-12-31",
        "val": value,
        "accn": accession,
        "fy": 2023,
        "fp": "FY",
        "form": "10-K",
        "filed": "2024-02-01",
    }
    net_income_entry = {**revenue_entry, "val": value * 0.2}
    facts = [
        XBRLFact(
            concept="Revenues",
            taxonomy="us-gaap",
            unit="USD",
            value=value,
            period_start=date(2023, 1, 1),
            period_end=date(2023, 12, 31),
            fiscal_year=2023,
            fiscal_period="FY",
            form="10-K",
            filed=date(2024, 2, 1),
            accession_number=accession,
        ),
        XBRLFact(
            concept="NetIncomeLoss",
            taxonomy="us-gaap",
            unit="USD",
            value=value * 0.2,
            period_start=date(2023, 1, 1),
            period_end=date(2023, 12, 31),
            fiscal_year=2023,
            fiscal_period="FY",
            form="10-K",
            filed=date(2024, 2, 1),
            accession_number=accession,
        ),
    ]
    raw = {
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [revenue_entry]}},
                "NetIncomeLoss": {"units": {"USD": [net_income_entry]}},
            }
        }
    }
    return XBRLCompanyFacts(facts=facts, raw=raw)


def _mock_xbrl_client(results: dict[str, XBRLCompanyFacts | DataUnavailable]) -> MagicMock:
    mock = MagicMock(spec=XBRLClient)

    async def _get(
        ticker: str, as_of: date, *, concepts: object = None
    ) -> XBRLCompanyFacts | DataUnavailable:
        return results[ticker]

    mock.get_company_facts_with_raw = AsyncMock(side_effect=_get)
    return mock


def _claim_batch_response(
    evidence_ids: list[str], *, tool_use_id: str = "toolu_1", claim_type: str = "evidence"
) -> SimpleNamespace:
    tool_input = {
        "claims": [
            {
                "id": str(uuid4()),
                "agent": "competitive",
                "statement": "Company revenue exceeds its peer's revenue.",
                "evidence_ids": evidence_ids,
                "claim_type": claim_type,
                "materiality": "medium",
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


async def test_run_returns_empty_when_ticker_not_in_peer_map() -> None:
    mock_xbrl = _mock_xbrl_client({})
    mock_sdk_client = MagicMock()
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = CompetitiveAgent(mock_xbrl, claude, store, make_settings(), peer_map={})

    claims = await agent.run("UNKNOWN", AS_OF)

    assert claims == []
    mock_xbrl.get_company_facts_with_raw.assert_not_called()
    mock_sdk_client.messages.create.assert_not_called()


async def test_run_produces_claims_when_one_peer_unavailable() -> None:
    mock_xbrl = _mock_xbrl_client(
        {
            TICKER: _company_facts(1_000_000, "acc-tick"),
            PEER: DataUnavailable(
                source="xbrl",
                identifier=PEER,
                as_of=AS_OF,
                reason="no data",
                attempted_at=datetime.now(UTC),
            ),
        }
    )
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)

    def _dynamic_response(*args: object, **kwargs: object) -> SimpleNamespace:
        real_evidence_id = store.all_evidence(source_type="xbrl_fact")[0].id
        return _claim_batch_response([str(real_evidence_id)])

    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = _dynamic_response
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    agent = CompetitiveAgent(mock_xbrl, claude, store, make_settings(), peer_map=_FIXTURE_PEER_MAP)

    claims = await agent.run(TICKER, AS_OF)

    assert len(claims) == 1
    assert store.claims() == claims
    # Only TICK's evidence exists (2 xbrl_fact + 1 computed); PEER1 was
    # skipped, not the whole run.
    fact_evidence = store.all_evidence(source_type="xbrl_fact")
    computed_evidence = store.all_evidence(source_type="computed")
    assert len(fact_evidence) == 2
    assert len(computed_evidence) == 1
    all_locations = [ev.location["company"] for ev in [*fact_evidence, *computed_evidence]]
    assert set(all_locations) == {TICKER}


async def test_run_produces_claims_for_ticker_and_peer() -> None:
    # WHY uuid4 is NOT patched here (unlike this file's other tests): this
    # run creates evidence for TWO companies (3 rows each) — pinning
    # uuid4() to one fixed value would make every row collide on the same
    # EvidenceStore dict key, silently overwriting one company's evidence
    # with the other's. The claim response is built dynamically from
    # whatever real evidence_id the store actually generated.
    mock_xbrl = _mock_xbrl_client(
        {
            TICKER: _company_facts(1_000_000, "acc-tick"),
            PEER: _company_facts(500_000, "acc-peer"),
        }
    )
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)

    def _dynamic_response(*args: object, **kwargs: object) -> SimpleNamespace:
        real_evidence_id = store.all_evidence(source_type="xbrl_fact")[0].id
        return _claim_batch_response([str(real_evidence_id)])

    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = _dynamic_response
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    agent = CompetitiveAgent(mock_xbrl, claude, store, make_settings(), peer_map=_FIXTURE_PEER_MAP)

    claims = await agent.run(TICKER, AS_OF)

    assert len(claims) == 1
    all_locations = {
        ev.location["company"]
        for ev in [
            *store.all_evidence(source_type="xbrl_fact"),
            *store.all_evidence(source_type="computed"),
        ]
    }
    assert all_locations == {TICKER, PEER}


@patch("backend.agents.competitive.uuid4")
async def test_run_retries_once_when_claim_cites_unknown_evidence(mock_uuid4: MagicMock) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id
    hallucinated_id = str(uuid4())

    mock_xbrl = _mock_xbrl_client(
        {
            TICKER: _company_facts(1_000_000, "acc-tick"),
            PEER: _company_facts(500_000, "acc-peer"),
        }
    )
    mock_sdk_client = MagicMock()
    bad_response = _claim_batch_response([hallucinated_id], tool_use_id="toolu_bad")
    good_response = _claim_batch_response([str(fixed_evidence_id)], tool_use_id="toolu_good")
    mock_sdk_client.messages.create.side_effect = [bad_response, good_response]
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = CompetitiveAgent(mock_xbrl, claude, store, make_settings(), peer_map=_FIXTURE_PEER_MAP)

    claims = await agent.run(TICKER, AS_OF)

    assert len(claims) == 1
    assert mock_sdk_client.messages.create.call_count == 2
    assert store.dropped_claims() == []


@patch("backend.agents.competitive.uuid4")
async def test_run_drops_claims_when_both_attempts_cite_unknown_evidence(
    mock_uuid4: MagicMock,
) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id

    mock_xbrl = _mock_xbrl_client(
        {
            TICKER: _company_facts(1_000_000, "acc-tick"),
            PEER: _company_facts(500_000, "acc-peer"),
        }
    )
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = [
        _claim_batch_response([str(uuid4())], tool_use_id="toolu_bad1"),
        _claim_batch_response([str(uuid4())], tool_use_id="toolu_bad2"),
    ]
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = CompetitiveAgent(mock_xbrl, claude, store, make_settings(), peer_map=_FIXTURE_PEER_MAP)

    claims = await agent.run(TICKER, AS_OF)

    assert claims == []
    dropped = store.dropped_claims()
    assert len(dropped) == 1
    assert dropped[0].agent == "competitive"
