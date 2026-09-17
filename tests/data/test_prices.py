"""Tests for backend.data.prices.

WHY unittest.mock, not respx: yfinance no longer talks plain httpx/requests
under the hood (it now uses curl_cffi for browser-TLS impersonation) — the
only reliable seam to mock is yfinance's own public `Ticker.history` method,
never the real library, never the real network.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
from backend.data.models import DataUnavailable
from backend.data.prices import PricesClient

from tests.data.conftest import make_settings


def _fake_history_frame(rows: list[tuple[str, float, float, float, float, int]]) -> pd.DataFrame:
    index = pd.to_datetime([r[0] for r in rows])
    return pd.DataFrame(
        {
            "Open": [r[1] for r in rows],
            "High": [r[2] for r in rows],
            "Low": [r[3] for r in rows],
            "Close": [r[4] for r in rows],
            "Volume": [r[5] for r in rows],
        },
        index=index,
    )


async def test_get_price_history_excludes_bars_after_as_of() -> None:
    frame = _fake_history_frame(
        [
            ("2024-06-27", 100, 101, 99, 100.5, 1_000_000),
            # yfinance's own end-boundary semantics have changed across
            # versions — this bar is deliberately included in the mocked
            # frame to prove OUR OWN defensive filter catches it,
            # regardless of what yfinance itself returns.
            ("2024-07-01", 105, 106, 104, 105.5, 2_000_000),
        ]
    )
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = frame

    with patch("backend.data.prices.yf.Ticker", return_value=mock_ticker):
        client = PricesClient(make_settings())
        result = await client.get_price_history("AAPL", date(2024, 6, 30))

    assert not isinstance(result, DataUnavailable)
    assert all(bar.date <= date(2024, 6, 30) for bar in result)
    assert len(result) == 1
    assert result[0].close == 100.5


async def test_get_price_history_empty_frame_is_data_unavailable() -> None:
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = pd.DataFrame()

    with patch("backend.data.prices.yf.Ticker", return_value=mock_ticker):
        client = PricesClient(make_settings())
        result = await client.get_price_history("NOTATICKER", date(2024, 6, 30))

    assert isinstance(result, DataUnavailable)
    assert result.source == "yfinance"


async def test_get_price_history_exception_is_retried_then_data_unavailable() -> None:
    mock_ticker = MagicMock()
    mock_ticker.history.side_effect = RuntimeError("Yahoo blocked us")

    with patch("backend.data.prices.yf.Ticker", return_value=mock_ticker):
        client = PricesClient(make_settings(data_retry_max_attempts=2))
        result = await client.get_price_history("AAPL", date(2024, 6, 30))

    assert isinstance(result, DataUnavailable)
    assert result.source == "yfinance"
    assert mock_ticker.history.call_count == 2


async def test_get_price_history_passes_raise_errors_true() -> None:
    frame = _fake_history_frame([("2024-06-27", 100, 101, 99, 100.5, 1_000_000)])
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = frame

    with patch("backend.data.prices.yf.Ticker", return_value=mock_ticker):
        client = PricesClient(make_settings())
        await client.get_price_history("AAPL", date(2024, 6, 30))

    _, kwargs = mock_ticker.history.call_args
    assert kwargs["raise_errors"] is True
