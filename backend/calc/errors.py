"""Errors shared by the valuation calc modules (wacc.py, beta.py, dcf.py)."""

from __future__ import annotations

from backend.calc.ratios import RatioInputError


class ValuationInputError(RatioInputError):
    """An input is invalid for a valuation formula: a non-positive capital
    base, a growth rate at or below -100%, too few return observations.

    WHY a subclass of RatioInputError: every valuation number is a
    `computed` evidence value just like a ratio, so a caller that already
    handles "this formula can't be evaluated on these inputs" catches both.
    Like RatioInputError, it exists so a formula never silently returns
    inf/nan (C6 applies to calculations too).
    """
