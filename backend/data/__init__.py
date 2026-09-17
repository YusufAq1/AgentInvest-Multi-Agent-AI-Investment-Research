"""All external I/O for AgentInvest lives here, and only here.

Every public method in this package takes an `as_of: date` and enforces it
in code (CLAUDE.md C3) — never relying solely on an upstream API's own date
filtering, since that can't be tested or trusted without a local check.
Missing data never raises past this package's own boundary for *expected*
misses (unknown ticker, no data in range, exhausted retries) — it returns
`DataUnavailable` (C6). A genuinely unexpected failure (misconfiguration, a
response shape that no longer parses) still propagates as an exception; see
`errors.py` for the reasoning.

Modules:
    models.py   Shared value types, including DataUnavailable.
    errors.py   Exception hierarchy (Transient/PermanentDataError, ...).
    retry.py    Shared async retry/backoff decorator (used by every client).
    http.py     Shared async HTTP client + rate limiter for direct SEC calls.
    cik.py      Ticker -> CIK resolution, shared by xbrl.py and news.py.
    cache.py    Local SQLite cache, keyed (source, args_hash, as_of).
    edgar.py    Filings (10-K/10-Q, section text) via edgartools.
    xbrl.py     Structured fundamentals via SEC XBRL companyfacts.
    prices.py   Daily price history via yfinance (Stooq deferred, ADR-0011).
    macro.py    Macro series (risk-free rate, CPI, real GDP) via FRED.
    news.py     8-K material events (RSS/GDELT deferred to Phase 4).
"""

from backend.data.errors import AgentInvestDataError
from backend.data.models import DataUnavailable

__all__ = ["AgentInvestDataError", "DataUnavailable"]
