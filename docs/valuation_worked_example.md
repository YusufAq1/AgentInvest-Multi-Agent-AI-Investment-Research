# Worked example: AAPL reverse DCF, as of 2024-06-30

This is the "hand-verified" part of Phase 6's exit criterion (CLAUDE.md
§15). Every number below is recomputed from the inputs printed by

```
uv run python scripts/valuation_demo.py --ticker AAPL --as-of 2024-06-30
```

using plain arithmetic that doesn't import anything from `backend/calc`.
It's an independent check of the module, not a second run of it. Run
date: 2026-10-02. Money is in USD.

## 1. Inputs (each traced to its source)

| Input | Value | Source |
|---|---|---|
| Revenue FY2023 (FYE 2023-09-30) | 383,285,000,000 | XBRL `RevenueFromContractWithCustomerExcludingAssessedTax`, 10-K 0000320193-23-000106 |
| Revenue FY2022 / FY2021 / FY2020 | 394,328M / 365,817M / 274,515M | same concept, 10-Ks |
| Operating income FY2023 / 22 / 21 | 114,301M / 119,437M / 108,949M | `OperatingIncomeLoss` |
| Capex FY2023 / 22 / 21 | 10,959M / 10,708M / 11,085M | `PaymentsToAcquirePropertyPlantAndEquipment` |
| D&A FY2023 / 22 / 21 | 11,519M / 11,104M / 11,284M | `DepreciationDepletionAndAmortization` |
| As-traded close, 2024-06-28 | 210.62 | yfinance, last trading day ≤ as_of (ADR-0021) |
| Shares outstanding (2024-04-19) | 15,334,082,000 | `dei:EntityCommonStockSharesOutstanding`, 10-Q filed 2024-05-03; no split since |
| Debt (2024-03-30) | 102,600M + 1,997M = 104,597M | `LongTermDebt` + `CommercialPaper` |
| Cash (2024-03-30) | 32,695M + 34,455M = 67,150M | cash & equivalents + current marketable securities |
| Interest expense FY2023 | 3,933M | `InterestExpense` |
| Risk-free rate | 4.29% | FRED DGS10, 2024-06-27 (the as-of-2024-06-30 vintage didn't yet include 06-28) |
| Beta | 1.2526 | OLS, 61 monthly total returns vs SPY |
| Tax rate, ERP, horizon | 21%, 5.00%, 10 years | config (printed as assumptions) |

## 2. Value drivers

**Operating margin**, the average of three years:
- 114,301 / 383,285 = 0.29821
- 119,437 / 394,328 = 0.30289
- 108,949 / 365,817 = 0.29782
- mean = **0.299642**

**Operating working capital**, (current assets − cash − current marketable
securities) − (current liabilities − current LTD − commercial paper):
- FY2023: (143,566 − 29,965 − 31,590) − (145,308 − 9,822 − 5,985) = 82,011 − 129,501 = **−47,490M**
- FY2020: (143,713 − 38,016 − 52,927) − (105,392 − 8,773 − 4,996) = 52,770 − 91,623 = **−38,853M**
- increase over 3 years = −47,490 − (−38,853) = **−8,637M**

**Incremental investment rate:**
- (Σcapex − ΣD&A + ΔNWC) / (Rev₂₀₂₃ − Rev₂₀₂₀)
- = (32,752 − 33,907 − 8,637) / (383,285 − 274,515)
- = −9,792 / 108,770 = **−0.090025**

A negative rate means growth *released* cash over this period. Capex ran
below D&A and working capital fell, so in this model growth comes with a
small cash inflow rather than a cost.

## 3. Cost of capital
- Cost of equity = 0.0429 + 1.2526 × 0.05 = **0.105530**
- Market cap = 210.62 × 15,334,082,000 = **3,229,664M**
- Cost of debt = 3,933 / 104,597 = **0.037601**
- WACC = (3,229,664 / 3,334,261) × 0.105530 + (104,597 / 3,334,261) × 0.037601 × 0.79 = 0.102220 + 0.000932 = **0.103151**
- Net debt = 104,597 − 67,150 = **37,447M**
- Target EV = 3,229,664 + 37,447 = **3,267,111M**

## 4. Solving for growth
Bisection on g until EV(g) = 3,267,111M gives **g = 18.555%**. At that
growth, in $bn:

| Year | Revenue | NOPAT (rev × 0.2996 × 0.79) | Investment (IIR × ΔRev) | FCFF | Discount factor | PV |
|---|---|---|---|---|---|---|
| 1 | 454.4 | 107.6 | −6.4 | 114.0 | 0.9065 | 103.3 |
| 2 | 538.7 | 127.5 | −7.6 | 135.1 | 0.8217 | 111.0 |
| 3 | 638.7 | 151.2 | −9.0 | 160.2 | 0.7449 | 119.3 |
| 4 | 757.2 | 179.2 | −10.7 | 189.9 | 0.6752 | 128.2 |
| 5 | 897.7 | 212.5 | −12.6 | 225.1 | 0.6121 | 137.8 |
| 6 | 1,064.2 | 251.9 | −15.0 | 266.9 | 0.5549 | 148.1 |
| 7 | 1,261.7 | 298.7 | −17.8 | 316.4 | 0.5030 | 159.2 |
| 8 | 1,495.8 | 354.1 | −21.1 | 375.2 | 0.4560 | 171.1 |
| 9 | 1,773.4 | 419.8 | −25.0 | 444.8 | 0.4133 | 183.8 |
| 10 | 2,102.4 | 497.7 | −29.6 | 527.3 | 0.3747 | 197.6 |

- PV of years 1–10 = **1,459.4**
- Terminal value = NOPAT₁₀ / WACC = 497.7 / 0.103151 = **4,824.7**, and its PV = 4,824.7 × 0.3747 = **1,807.7**
- EV = 1,459.4 + 1,807.7 = **3,267.1bn**, which equals the target

## 5. Result and cross-check

| | Hand calculation | `scripts/valuation_demo.py` |
|---|---|---|
| WACC | 0.103151 | 0.103152 |
| Implied revenue growth | 18.555% | 18.56% |
| Break-even IIR, 0.299642 × 0.79 × 1.103151 / 0.103151 | 2.5316 | 2.5316 |
| 3-year revenue CAGR, (383,285 / 274,515)^(1/3) − 1 | 11.77% | 11.77% |
| 5-year revenue CAGR, (383,285 / 265,595)^(1/5) − 1 | 7.61% | 7.61% |

The last-digit WACC difference is beta: the hand calculation uses the
printed 1.2526, while the module uses full precision.

**Reading it:** at the 2024-06-28 price, the model says the market was
pricing in about 18.6% a year revenue growth for ten years, at roughly
constant margins. That's well above Apple's own 11.8% (3-year) and 7.6%
(5-year) history. The IIR (−0.09) is far below break-even (2.53), so in
this model growth creates value, and a higher price means higher implied
growth. Whether 18.6% is plausible is exactly the question Phase 7's
Bull/Bear/Critic layer is for. This page only shows that the number is
computed correctly.

**Why the number is high, and what it depends on:**
- **Margin expansion gets read as growth.** The model holds margin at 30%,
  so any expected margin improvement shows up as growth instead.
- **The terminal value is conservative.** It allows no growth after year
  10, so all expected growth has to fit in the ten-year window.
- **The WACC sensitivity is large.** At a WACC 1 point lower (9.32%), the
  grid's 15% row is already worth $186 a share.

*AgentInvest is an experimental research tool. Its output is generated by AI
systems, may contain errors, and is not investment advice. Do not make
investment decisions on the basis of this output.*
