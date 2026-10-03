# ADR-0006: Reverse DCF on Mauboussin value drivers, not a forward "fair value"

## Status
Accepted — 2026-10-02 (Increment 6a: the pure calculation layer)

## Context
CLAUDE.md §8 rules out a single "fair value" number: a forward DCF whose
inputs are guessed produces false precision, an answer that is entirely a
function of made-up assumptions, printed to two decimals. It asks instead
for:
- a **reverse DCF**: take the market price as given, and solve for the
  revenue growth the market implies
- a **growth × WACC sensitivity grid**
- **WACC from free data**, with every simplification printed

The reference is Alfred Rappaport and Michael J. Mauboussin,
*Expectations Investing: Reading Stock Prices for Better Returns* (Harvard
Business School Press, 2001; revised edition, Columbia Business School
Publishing, 2021).

## Decision

### The model: value drivers, with growth paid for by reinvestment
For constant revenue growth g over an explicit horizon of N years
(`backend/calc/dcf.py`):

```
Rev_t   = Rev_0 · (1 + g)^t
NOPAT_t = Rev_t · operating_margin · (1 − tax_rate)
Inv_t   = IIR · (Rev_t − Rev_{t−1})          IIR = incremental investment rate
FCFF_t  = NOPAT_t − Inv_t
EV      = Σ FCFF_t / (1+WACC)^t  +  [NOPAT_N / WACC] / (1+WACC)^N
```

- **Reinvestment tied to growth.** Each extra dollar of revenue requires
  IIR dollars of net investment (capex − D&A + working capital). This is
  the owner's choice over a "constant free-cash-flow margin" model, in
  which growth is free and the implied growth is therefore biased low.
- **Perpetuity terminal value** (`NOPAT_N / WACC`, no growth after year N).
  This is Mauboussin's assumption that post-horizon growth earns exactly
  its cost of capital, so it adds no value. It removes the terminal-growth
  input that, in a Gordon-growth DCF, often supplies most of the value
  from one guessed number.
- **End-of-year discounting.** The standard convention, and it keeps the
  hand-worked tests readable.

### What the model implies about growth (found while building it)
Each extra dollar of revenue costs IIR now and earns margin·(1 − tax) from
that year on. Under this timing, growth is value-neutral at

```
IIR* = margin · (1 − tax) · (1 + WACC) / WACC
```

and EV is **monotonic** in g, in a direction set by IIR versus IIR*:
- **IIR < IIR\*: growth creates value.** A higher price implies higher
  growth.
- **IIR > IIR\*: growth destroys value.** EV *falls* as g rises, so a higher
  price implies *lower* growth. This is a real conclusion the report must
  state, and it's why the result carries `growth_creates_value`.
- **IIR = IIR\*: EV is flat** at NOPAT₀/WACC, and the price says nothing
  about growth.

**Correction to the approved plan.** The plan assumed EV could rise and
then fall, giving several implied growth rates, and specified an
"ambiguous" flag listing every root. Testing showed that can't happen for
constant drivers. A numeric sweep over horizons 1–20, WACC 3–15% and IIR
from 0.3× to 5× break-even found only one non-monotonic case: exact
break-even, where EV is flat and float noise flips the sign. That is now a
named property test (`test_ev_is_monotonic_in_growth_with_direction_set_by_break_even`).
The "ambiguous" path was replaced with:
- `growth_creates_value` on every result
- an explicit **value-neutral** `NoImpliedGrowth`, detected when EV's
  spread across the range is within `flat_tolerance` of its size
- a **hard error** if the scan ever finds more than one crossing, because
  for this model that means a bug or an unvalidated model change, and
  quietly picking a root would hide it

### The solver: scan, then bisect
`solve_implied_growth` evaluates EV on an even grid (−30% to +60% in 0.5%
steps, from config in 6b), finds the one adjacent pair where EV − target
changes sign, and bisects it to `tolerance`.
- Why not Newton's method: it needs a derivative and can jump out of
  range.
- Why not bare bisection on the whole range: it can't distinguish "no
  root" from "flat", and it would silently break if the model ever became
  non-monotonic.

A target outside the range returns `NoImpliedGrowth`, naming whether it
is above the highest or below the lowest EV in range. **It never returns
a nearest-guess number** (C6). No scipy: pure Python is enough, and every
step is readable.

### WACC and beta
`backend/calc/wacc.py`: CAPM `Rₑ = R_f + β·ERP`, after-tax `R_d·(1 − t)`,
and `WACC = (E/V)·Rₑ + (D/V)·R_d·(1 − t)`. `backend/calc/beta.py`: OLS
beta, computed as `cov/var` on monthly simple returns.
- Monthly returns use the last close of each calendar month.
- A missing month never becomes a two-month return.
- At least `min_observations` overlapping months are required (36 in
  config).

How each input is *sourced* is decided in Increment 6b, and its
simplifications are printed there, each one listed in that increment's
assumptions output:
- the 10-year Treasury as R_f
- a configured ERP
- statutory tax
- book debt in place of market debt
- interest ÷ debt as R_d

### Shared conventions
- Every scalar result is a `RatioResult` (name, formula, inputs, value),
  the same shape as `ratios.py`. So in Increment 6c each number becomes a
  `computed` Evidence row that CI recomputes from its cited inputs.
- Invalid inputs raise `ValuationInputError`, a subclass of
  `RatioInputError`. A formula never returns inf or nan.
- `revenue_cagr` joined `ratios.py`: it's the history the implied growth
  is judged against.

## Alternatives considered
- **Forward DCF with a fair value.** Rejected by §8: false precision.
- **Constant FCF margin.** Simpler, but growth is free, which biases the
  implied growth low. Rejected by the owner.
- **Gordon-growth terminal value.** It needs a terminal growth rate that
  usually dominates the answer. The perpetuity method is the reference's
  convention.
- **ROIC-linked reinvestment** (`reinvestment = g / ROIC × NOPAT`). This is
  elegant, but invested capital is negative or tiny for buyback-heavy
  companies (book equity close to zero), which makes ROIC meaningless
  exactly where the system needs to work.
- **scipy's `brentq`.** A dependency for about 15 lines of bisection, and
  it hides the scan, which is what detects the flat and out-of-range cases.

## Consequences
- **Easy now:**
  - Every valuation number is reproducible from explicit inputs.
  - The tests carry hand-worked expected values.
  - The implied growth comes with its direction (`growth_creates_value`)
    and the break-even rate.
- **Hard or limiting:**
  - Margin, IIR and WACC are constant over the horizon. A company
    expected to expand margins is modelled as if its whole story is
    revenue growth, so the implied growth absorbs margin expectations.
    This will be stated in the report.
  - IIR is estimated from three years of history in 6b, which is noisy,
    and undefined when revenue fell (6b uses a labelled assumed value
    then).
- **Revisit** if a fade model (growth or margin decaying toward a steady
  state) is ever added. The scan-first solver and its multiple-crossing
  error are there for that day.
