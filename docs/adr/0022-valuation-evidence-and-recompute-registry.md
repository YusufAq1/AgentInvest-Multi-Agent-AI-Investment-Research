# ADR-0022: Valuation evidence that recomputes in CI, via a registry and linear-combination citations

## Status
Accepted — 2026-10-03

## Context
Phase 6c puts the reverse DCF into the graph as a Valuation Agent whose
claims Bull, Bear and the Judge will cite in Phase 7. CLAUDE.md requires:
- every material claim to cite evidence that resolves (§6)
- numbers never to come from an LLM (C4)

Phase 2's answer for derived numbers was `computed` evidence. The CI
citation check (`backend/evidence/validation.py`) recomputes a ratio from
the rows it cites and fails on a mismatch, and it deliberately refuses
self-reported inputs.

The Phase 2 validator could only express "a ratio of single XBRL facts",
and a test enforces that. Valuation numbers break this in three ways:
1. **They chain.** WACC depends on cost of equity, which depends on beta
   and the risk-free rate. Implied growth depends on WACC, margin,
   reinvestment and the target EV.
2. **Their inputs are combinations of facts:**
   - a 3-year capex sum
   - revenue change, `FY0 − FY3`
   - total debt, `LongTermDebt + CommercialPaper`
   - shares × split factor
   - operating working capital, six signed facts at two dates
3. **Some inputs aren't XBRL.** The as-traded price and beta come from
   yfinance, the risk-free rate from FRED, and the ERP, tax rate and horizon
   are configured assumptions.

## Decision

**1. One registry of recomputable calculations** (`backend/calc/registry.py`,
`COMPUTED_FUNCS`). For each name it declares:
- the function
- the source types its inputs may cite
  - ratios: `xbrl_fact` only, which keeps the Phase 2 rule and its test
  - valuation: also `price_series`, `macro` and `computed`, which allows
    chains
- which inputs may be **constants** stored in the row. These are only
  configured assumptions: ERP, tax rate, horizon, the solver's settings,
  the credit spread, and an assumed investment rate's placeholder. Any
  other self-reported input is rejected.

**2. Citations can be signed linear combinations.** A computed row's
`input_evidence_ids` maps each parameter either to one row id, or to
`[[coefficient, row_id], ...]`, resolved as Σ coefficient × value.
- This expresses every aggregate above without adding intermediate rows.
- Every number is still taken from resolved evidence, never from the row
  claiming it.

**3. Market and macro rows are checked for self-consistency.** There's no
archived yfinance or FRED response to check a quote against. So, like the
8-K rows in ADR-0017, the quote is a canonical rendering of the row's own
fields (`backend/evidence/market_quotes.py`, using `repr` floats so
re-rendering is exact). The validator re-renders and compares, and also
checks:
- beta = covariance / variance
- the decimal rate = percent / 100

**4. Implied growth is a recomputable number.** `implied_revenue_growth`
re-runs the solver from the cited rows and the declared solver constants.
When no growth fits, an `implied_growth_solutions = 0` row carries the
reason, so "the price implies no growth rate in range" is itself citable
and recomputable.

**5. Claude sees only headline rows.** About 15 labelled rows, out of
roughly 47 stored. They're the conclusions, and those rows cite the facts.
The prompt forbids computing any new number and allows comparison in words
only. A run whose required inputs are missing raises
`ValuationUnavailableError` with every reason. The orchestrator's bulkhead
records that as a failed outcome, which is visible rather than a silent
empty success.

**6. Shared XBRL rows.** `make_xbrl_fact_evidence` moved from the Financial
Agent into `backend/agents/xbrl_facts.py` so both agents build
byte-identical rows. The Financial Agent's tests are unchanged.

**Verified:**
- AAPL's and NVDA's real evidence sets (47 rows each, as of 2024-06-30)
  pass the full CI citation sweep with zero failures, and nothing is dated
  after as_of.
- Tampering tests:
  - Editing the stored operating margin fails its own recompute **and**
    the implied growth's, because that row recomputes from the margin
    row.
  - Editing the price fails its quote **and** the market cap.

## Alternatives considered
- **Store valuation numbers as `computed` rows without recompute** (trust
  the code that made them). Rejected: that's the "please be accurate"
  approach §6 exists to replace, and CI would no longer catch a
  regression in the valuation chain.
- **An intermediate row for every aggregate** (a "3-year capex sum" row,
  a "revenue change" row, and so on). This works, but roughly doubles the
  row count and adds a calculation name per aggregate. Linear combinations
  express the same thing at the citation.
- **Let any computed row take any self-reported constant.** Rejected: a row
  could then fabricate its inputs and value together, which is exactly what
  `_validate_computed` was designed to prevent. Constants are whitelisted
  per calculation.
- **Archive yfinance and FRED responses so market rows get real
  containment checks.** That's the stronger answer, and the right future
  step. It needs a response store, which is a Phase-later concern. The
  self-consistency check is documented as weaker.
- **Show Claude every fact.** Rejected on cost (§5) and because Claude
  should be citing conclusions.

## Consequences
- **Easy now:** every valuation number a Phase 7 claim cites reproduces
  from source data in CI. Bull and Bear can cite "implied growth vs 3-year
  CAGR" with that guarantee.
- **Known limits:**
  - **Market rows only prove internal consistency.** They don't prove the
    vendor value was right when fetched.
  - **Beta's 60-month return series isn't stored.** Only its moments are,
    so beta recomputes from its moments, not from prices.
  - **The sensitivity grid isn't evidence yet.** It's printed by the demo
    but not stored; Phase 7's Judge may need it.
- **Adding a computed calculation is now a registry entry.** A calculation
  that isn't registered can't be stored as evidence that validates, which
  is the intended friction.
