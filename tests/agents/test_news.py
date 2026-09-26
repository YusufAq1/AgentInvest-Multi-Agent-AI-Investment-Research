"""Tests for backend.agents.news.NewsAgent.

No real network calls: NewsClient is mocked entirely, and ClaudeClient
wraps a mocked Anthropic SDK client (matching tests/test_llm.py's
discipline) so the real forced-tool-use + retry mechanics run for real
against fake SDK responses.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from backend.agents.news import NewsAgent, _event_quote
from backend.core.llm import ClaudeClient
from backend.data.models import DataUnavailable, EightKEvent
from backend.data.news import NewsClient
from backend.evidence.store import EvidenceStore

from tests.conftest import make_settings

AS_OF = date(2024, 6, 30)
TICKER = "AAPL"

_EVENT = EightKEvent(
    accession_number="0000320193-24-000050",
    filing_date=date(2024, 5, 2),
    items=["2.02", "9.01"],
    item_labels=[
        "Results of Operations and Financial Condition",
        "Financial Statements and Exhibits",
    ],
)


def _mock_news_client(result: list[EightKEvent] | DataUnavailable) -> MagicMock:
    mock = MagicMock(spec=NewsClient)
    mock.get_8k_events = AsyncMock(return_value=result)
    return mock


def _claim_batch_response(
    evidence_ids: list[str], *, tool_use_id: str = "toolu_1", claim_type: str = "evidence"
) -> SimpleNamespace:
    tool_input = {
        "claims": [
            {
                "id": str(uuid4()),
                "agent": "news",
                "statement": "The company filed a Form 8-K disclosing results of operations.",
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


def test_event_quote_is_deterministic_and_derived_only_from_real_fields() -> None:
    quote = _event_quote(_EVENT)

    assert quote == (
        "8-K filed 2024-05-02 (accession 0000320193-24-000050): "
        "2.02 (Results of Operations and Financial Condition); "
        "9.01 (Financial Statements and Exhibits)"
    )


async def test_run_returns_empty_when_data_unavailable() -> None:
    unavailable = DataUnavailable(
        source="sec_8k",
        identifier=TICKER,
        as_of=AS_OF,
        reason="no data",
        attempted_at=datetime.now(UTC),
    )
    mock_news = _mock_news_client(unavailable)
    mock_sdk_client = MagicMock()
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = NewsAgent(mock_news, claude, store, make_settings())

    claims = await agent.run(TICKER, AS_OF)

    assert claims == []
    mock_sdk_client.messages.create.assert_not_called()


@patch("backend.agents.news.uuid4")
async def test_run_produces_claims_referencing_real_evidence(mock_uuid4: MagicMock) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id

    mock_news = _mock_news_client([_EVENT])
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.return_value = _claim_batch_response([str(fixed_evidence_id)])
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = NewsAgent(mock_news, claude, store, make_settings())

    claims = await agent.run(TICKER, AS_OF)

    assert len(claims) == 1
    assert claims[0].evidence_ids == [fixed_evidence_id]
    assert store.claims() == claims
    assert len(store.all_evidence(source_type="news")) == 1
    mock_news.get_8k_events.assert_awaited_once_with(
        TICKER, AS_OF, lookback_days=make_settings().news_agent_lookback_days
    )


@patch("backend.agents.news.uuid4")
async def test_run_retries_once_when_claim_cites_unknown_evidence(mock_uuid4: MagicMock) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id
    hallucinated_id = str(uuid4())

    mock_news = _mock_news_client([_EVENT])
    mock_sdk_client = MagicMock()
    bad_response = _claim_batch_response([hallucinated_id], tool_use_id="toolu_bad")
    good_response = _claim_batch_response([str(fixed_evidence_id)], tool_use_id="toolu_good")
    mock_sdk_client.messages.create.side_effect = [bad_response, good_response]
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = NewsAgent(mock_news, claude, store, make_settings())

    claims = await agent.run(TICKER, AS_OF)

    assert len(claims) == 1
    assert claims[0].evidence_ids == [fixed_evidence_id]
    assert mock_sdk_client.messages.create.call_count == 2
    assert store.dropped_claims() == []


@patch("backend.agents.news.uuid4")
async def test_run_drops_claims_when_both_attempts_cite_unknown_evidence(
    mock_uuid4: MagicMock,
) -> None:
    fixed_evidence_id = uuid4()
    mock_uuid4.return_value = fixed_evidence_id

    mock_news = _mock_news_client([_EVENT])
    mock_sdk_client = MagicMock()
    mock_sdk_client.messages.create.side_effect = [
        _claim_batch_response([str(uuid4())], tool_use_id="toolu_bad1"),
        _claim_batch_response([str(uuid4())], tool_use_id="toolu_bad2"),
    ]
    claude = ClaudeClient(make_settings(), client=mock_sdk_client)
    store = EvidenceStore(run_id=uuid4(), as_of=AS_OF)
    agent = NewsAgent(mock_news, claude, store, make_settings())

    claims = await agent.run(TICKER, AS_OF)

    assert claims == []
    dropped = store.dropped_claims()
    assert len(dropped) == 1
    assert dropped[0].agent == "news"
    assert dropped[0].reason
