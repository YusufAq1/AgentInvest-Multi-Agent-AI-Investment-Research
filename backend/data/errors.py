"""Exception hierarchy for backend.data.

WHY these exist alongside `DataUnavailable` (see models.py): CLAUDE.md C6
says missing data must return an explicit marker that "propagates into the
report" — that's a return-value contract, not an exception. But §16
separately requires "custom exceptions per failure mode" with retry logic
that "distinguishes transient from permanent errors." Both are true at
once: every public method in backend/data *catches* its own expected
failure modes (rate limit exhausted, ticker not found, no data in the
requested window) and returns `T | DataUnavailable` for those — but a
genuinely unexpected failure (misconfigured identity, a response shape
that no longer parses) is deliberately left to propagate as a real
exception, so a bug can never masquerade as an ordinary missing-data case.
`TransientDataError`/`PermanentDataError` exist purely to drive that
internal retry decision (see retry.py) before a method decides whether to
catch-and-convert or let something propagate.
"""

from backend.core.llm import AgentInvestError


class AgentInvestDataError(AgentInvestError):
    """Base class for all backend.data exceptions."""


class TransientDataError(AgentInvestDataError):
    """A failure that might succeed on retry (timeout, 429, 5xx, a dropped
    connection). Safe to retry with backoff — see retry.with_retry.
    """


class PermanentDataError(AgentInvestDataError):
    """A failure that will not succeed on retry. Never retried."""


class EdgarIdentityNotConfiguredError(PermanentDataError):
    """`Settings.edgar_identity` is missing or empty — a configuration bug,
    not a data-availability problem. Deliberately not converted to
    DataUnavailable: it means the whole system is misconfigured, not that
    one query happened to come up empty.
    """


class SecIdentityRejectedError(PermanentDataError):
    """SEC returned 403 despite a User-Agent being set — almost certainly a
    malformed or blocked identity string, not a per-query data gap.
    """


class CikNotFoundError(PermanentDataError):
    """No CIK found for a ticker (unknown, delisted, or mistyped). This
    *is* an expected, per-query outcome — callers in xbrl.py/news.py catch
    this specific exception and convert it to DataUnavailable; it's kept as
    a distinct exception type (rather than a plain None return) so it
    composes with the same retry/error-propagation machinery as everything
    else in this module.
    """


class UpstreamSchemaError(PermanentDataError):
    """A response parsed as JSON but didn't match the shape this code
    expects (e.g. a field renamed upstream). Deliberately not converted to
    DataUnavailable: this means our parsing code is out of date, not that
    the data is unavailable.
    """
