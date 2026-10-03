"""Tests for backend.data.prices.

WHY unittest.mock, not respx: yfinance no longer talks plain httpx/requests
under the hood (it now uses curl_cffi for browser-TLS impersonation) — the
only reliable seam to mock is yfinance's own public `Ticker.history` method,
never the real library, never the real network.
"""

import asyncio
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from backend.data.models import DataUnavailable, PriceBar, StockSplit
from backend.data.prices import (
    PricesClient,
    split_factor_after,
    split_factor_between,
    unadjust_for_splits,
)

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


def test_history_requests_end_one_day_after_as_of() -> None:
    """yfinance's `end` is exclusive, so end=as_of would drop the as_of
    day's own bar."""
    frame = _fake_history_frame([("2024-06-28", 100, 101, 99, 100.5, 1_000_000)])
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = frame

    with patch("backend.data.prices.yf.Ticker", return_value=mock_ticker):
        asyncio.run(PricesClient(make_settings()).get_price_history("AAPL", date(2024, 6, 28)))

    assert mock_ticker.history.call_args.kwargs["end"] == date(2024, 6, 29)


# ---- As-traded prices and splits (ADR-0021) ------------------------------

_NVDA_SPLITS = [
    StockSplit(date=date(2021, 7, 20), ratio=4.0),
    StockSplit(date=date(2024, 6, 10), ratio=10.0),
]


def test_split_factor_after_counts_only_later_splits() -> None:
    """A bar on the split date already trades post-split, so only splits
    strictly after the bar count."""
    assert split_factor_after(_NVDA_SPLITS, date(2024, 5, 31)) == 10.0
    assert split_factor_after(_NVDA_SPLITS, date(2024, 6, 10)) == 1.0
    assert split_factor_after(_NVDA_SPLITS, date(2021, 1, 4)) == 40.0


def test_split_factor_between_is_half_open() -> None:
    assert split_factor_between(_NVDA_SPLITS, date(2024, 5, 24), date(2024, 6, 28)) == 10.0
    assert split_factor_between(_NVDA_SPLITS, date(2024, 6, 10), date(2024, 6, 28)) == 1.0
    assert split_factor_between(_NVDA_SPLITS, date(2024, 5, 24), date(2024, 6, 10)) == 10.0


def test_unadjust_for_splits_restores_the_traded_price() -> None:
    """NVDA 2024-05-31: yfinance reports 109.633 split-adjusted. One later
    10:1 split → 109.633 × 10 = 1,096.33, the actual close. Volume goes the
    other way (÷10). The post-split bar is unchanged."""
    bars = [
        PriceBar(
            date=date(2024, 5, 31),
            open=109.0,
            high=111.0,
            low=108.0,
            close=109.633,
            volume=500_000_000,
        ),
        PriceBar(
            date=date(2024, 6, 10),
            open=120.0,
            high=122.0,
            low=119.0,
            close=121.79,
            volume=300_000_000,
        ),
    ]
    pre, post = unadjust_for_splits(bars, _NVDA_SPLITS)
    assert pre.close == pytest.approx(1096.33)
    assert pre.volume == 50_000_000
    assert post == bars[1]


def _split_series(splits: list[tuple[str, float]]) -> pd.Series:
    index = pd.to_datetime([d for d, _ in splits]).tz_localize("America/New_York")
    return pd.Series([r for _, r in splits], index=index)


def test_as_traded_history_fetches_unadjusted_closes_and_multiplies_back() -> None:
    frame = _fake_history_frame([("2024-05-31", 109, 111, 108, 109.633, 500_000_000)])
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = frame
    mock_ticker.splits = _split_series([("2021-07-20", 4.0), ("2024-06-10", 10.0)])

    with patch("backend.data.prices.yf.Ticker", return_value=mock_ticker):
        result = asyncio.run(
            PricesClient(make_settings()).get_price_history(
                "NVDA", date(2024, 5, 31), adjustment="as_traded"
            )
        )

    assert not isinstance(result, DataUnavailable)
    assert result[0].close == pytest.approx(1096.33)
    # Dividend-unadjusted fetch; split adjustment is undone by us.
    assert mock_ticker.history.call_args.kwargs["auto_adjust"] is False


def test_total_return_history_keeps_auto_adjust() -> None:
    frame = _fake_history_frame([("2024-05-31", 109, 111, 108, 109.3, 1)])
    mock_ticker = MagicMock()
    mock_ticker.history.return_value = frame

    with patch("backend.data.prices.yf.Ticker", return_value=mock_ticker):
        asyncio.run(PricesClient(make_settings()).get_price_history("NVDA", date(2024, 5, 31)))

    assert mock_ticker.history.call_args.kwargs["auto_adjust"] is True


def test_get_splits_never_returns_a_split_after_as_of() -> None:
    mock_ticker = MagicMock()
    mock_ticker.splits = _split_series([("2021-07-20", 4.0), ("2024-06-10", 10.0)])

    with patch("backend.data.prices.yf.Ticker", return_value=mock_ticker):
        result = asyncio.run(PricesClient(make_settings()).get_splits("NVDA", date(2024, 5, 31)))

    assert result == [StockSplit(date=date(2021, 7, 20), ratio=4.0)]
