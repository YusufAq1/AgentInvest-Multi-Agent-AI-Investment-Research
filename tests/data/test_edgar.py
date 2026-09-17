"""Tests for backend.data.edgar.

WHY unittest.mock, not respx: edgartools' public API is synchronous and
opaque (see edgar.py's module docstring) — the only thing worth testing
here is OUR code (the defensive as_of re-check, error mapping), never
edgartools itself or the real network.
"""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.data.edgar import EdgarClient
from backend.data.errors import EdgarIdentityNotConfiguredError
from backend.data.models import DataUnavailable, Filing
from edgar._filings import Filing as EdgarFiling

from tests.data.conftest import make_settings


def _fake_edgar_filing(accession_number: str, form: str, filing_date: str, cik: int) -> object:
    return SimpleNamespace(
        accession_number=accession_number, form=form, filing_date=filing_date, cik=cik
    )


def test_constructor_raises_if_identity_not_configured() -> None:
    try:
        EdgarClient(make_settings(edgar_identity=""))
    except EdgarIdentityNotConfiguredError:
        pass
    else:
        raise AssertionError("expected EdgarIdentityNotConfiguredError")


@patch("backend.data.edgar.Company")
async def test_get_filings_excludes_items_after_as_of_despite_correct_filter_string(
    mock_company_cls: MagicMock,
) -> None:
    # The filter string passed to edgartools is correct (":2024-06-30"),
    # but the mocked library returns an item outside that range anyway —
    # proving our own re-check (not blind trust in the library) is what
    # actually enforces as_of, exactly as edgar.py's docstring claims.
    mock_company = MagicMock()
    mock_company.get_filings.return_value.head.return_value = [
        _fake_edgar_filing("PRE-AS-OF", "10-K", "2024-02-01", 320193),
        _fake_edgar_filing("POST-AS-OF", "10-K", "2024-08-01", 320193),
    ]
    mock_company_cls.return_value = mock_company

    client = EdgarClient(make_settings())
    result = await client.get_filings("AAPL", date(2024, 6, 30))

    assert not isinstance(result, DataUnavailable)
    assert all(f.filing_date <= date(2024, 6, 30) for f in result)
    assert not any(f.accession_number == "POST-AS-OF" for f in result)
    assert any(f.accession_number == "PRE-AS-OF" for f in result)

    _, kwargs = mock_company.get_filings.call_args
    assert kwargs["filing_date"] == ":2024-06-30"


@patch("backend.data.edgar.Company")
async def test_get_filings_pads_cik(mock_company_cls: MagicMock) -> None:
    mock_company = MagicMock()
    mock_company.get_filings.return_value.head.return_value = [
        _fake_edgar_filing("ACC-1", "10-K", "2024-02-01", 320193),
    ]
    mock_company_cls.return_value = mock_company

    client = EdgarClient(make_settings())
    result = await client.get_filings("AAPL", date(2024, 6, 30))

    assert not isinstance(result, DataUnavailable)
    assert result[0].cik == "0000320193"


@patch("backend.data.edgar.Company")
async def test_get_filings_no_results_is_data_unavailable(mock_company_cls: MagicMock) -> None:
    mock_company = MagicMock()
    mock_company.get_filings.return_value.head.return_value = []
    mock_company_cls.return_value = mock_company

    client = EdgarClient(make_settings())
    result = await client.get_filings("AAPL", date(2024, 6, 30))

    assert isinstance(result, DataUnavailable)
    assert result.source == "edgar"


async def test_get_filing_section_rejects_stale_filing_against_new_as_of() -> None:
    client = EdgarClient(make_settings())
    filing = Filing(
        accession_number="ACC-1", form="10-K", filing_date=date(2024, 8, 1), cik="0000320193"
    )

    result = await client.get_filing_section(filing, "Item 1A", as_of=date(2024, 6, 30))

    assert isinstance(result, DataUnavailable)
    assert result.source == "edgar"


@patch("backend.data.edgar.edgar_find")
async def test_get_filing_section_returns_section_text(mock_find: MagicMock) -> None:
    real_filing = EdgarFiling(
        cik=320193,
        company="Apple Inc.",
        form="10-K",
        filing_date="2024-02-01",
        accession_no="ACC-1",
    )
    real_filing.obj = lambda: {"Item 1A": "Risk factors text."}  # type: ignore[method-assign]
    mock_find.return_value = real_filing

    client = EdgarClient(make_settings())
    filing = Filing(
        accession_number="ACC-1", form="10-K", filing_date=date(2024, 2, 1), cik="0000320193"
    )
    result = await client.get_filing_section(filing, "Item 1A", as_of=date(2024, 6, 30))

    assert result == "Risk factors text."
