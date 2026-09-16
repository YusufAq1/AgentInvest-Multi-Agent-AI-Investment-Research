## The core idea, restated

You give it a ticker and a date (`as_of`). It doesn't ask an LLM "is this a good stock" — it runs a pipeline where specialist agents gather evidence, two agents argue opposite sides using only that evidence, a critic attacks both sides, and a judge produces a final position with citations and a list of things that would prove it wrong. The LLM never touches arithmetic and never asserts a fact without a receipt.

---

## Stage 0: Input

**What happens:** You call the API with `{ticker: "NVDA", as_of: "2026-06-30"}`. A `research_runs` row is created immediately, with `as_of_date` as a required, non-nullable column.

**Why `as_of` exists at all:** This is the single most important design decision in the whole project, and it's easy to underestimate. If you only ever run research "as of today," you never notice the system quietly leaking future information into past analysis. But the moment you want to backtest this system — "would it have called NVDA correctly in January 2025?" — every piece of data access needs to already respect a point-in-time boundary, or the whole exercise is fraudulent. Retrofitting that later means touching every function that ever touches data. So it's there from day one even though the MVP only ever passes `as_of=today`.

**Tool:** Just Postgres and FastAPI here — nothing interesting yet, but the schema decision (`as_of_date NOT NULL`) is doing real work.

---

## Stage 1: The data layer fetches raw material

**What happens:** Before any agent runs, the Research Manager needs data to hand them. The data layer pulls, for this ticker, as of this date:

- **Structured financials** from SEC's XBRL `companyfacts` API — revenue, net income, assets, liabilities, EPS, going back years, already numeric and standardized.
- **Filing text** — 10-Ks, 10-Qs, 8-Ks — via `edgartools`, which turns a filing into an object where `filing["Item 1A"]` gives you Risk Factors directly instead of a 200-page blob.
- **Prices** via `yfinance`, with Stooq as a fallback if Yahoo throttles.
- **Macro data** (risk-free rate, CPI) via FRED.
- **Recent 8-Ks** as the material-events feed.

Every one of these calls takes `as_of` as an argument and filters out anything published after it.

**Why split fundamentals (XBRL) from filing text (RAG) at all:** This is a decision people get wrong constantly in RAG projects. It would be *easier* to just dump the whole 10-K into a vector store and ask "what was NVDA's revenue?" — and it would be *wrong*, because embeddings retrieve semantically similar text, not exact numbers, and an LLM reading a retrieved paragraph can transpose a digit. SEC already publishes every financial figure as structured, machine-readable XBRL data, for free. So the rule is: **numbers come from XBRL, always; RAG is only ever used for qualitative, narrative text** — risk factors, MD&A commentary, competitive discussion — the kind of content that genuinely doesn't exist as structured data. This single decision eliminates an entire category of hallucination before any LLM is even involved.

**Why cache aggressively here:** SEC EDGAR enforces a hard 10 requests/second limit with a required identifying User-Agent header — omit it and you get a 403. yfinance has no rate limit contract at all; it's an unofficial scrape of a redesigned Yahoo frontend that periodically breaks. Both facts point the same direction: fetch once, store forever (financial filings don't change retroactively), and never make a live network call inside the hot path of a research run if you can serve it from cache.

---

## Stage 2: The Research Manager plans

**What happens:** The Manager — an LLM call, cheap model — looks at the ticker and available data and decides which specialist agents actually need to run and with what focus. For a stable large-cap this might be a straightforward five-agent fan-out; for a company that just had an 8-K filed yesterday, it might prioritize the News agent and flag urgency.

**Why an LLM plans this instead of it being hardcoded:** Because "which agents to run and what to tell them to focus on" is exactly the kind of judgment call that benefits from reading context — a company mid-acquisition needs different emphasis than one that just reported steady earnings. But note the boundary: the Manager decides *what to investigate*, never *what the numbers are*. Planning is qualitative judgment; that's squarely LLM territory.

**Model used:** Haiku. This is a routing/classification task, not deep reasoning — it doesn't need the expensive model, and it runs on every single request.

---

## Stage 3: Specialist agents run in parallel

**What happens:** Financial, Filings, News, Competitive, and Valuation agents run concurrently (`asyncio.gather`, with a semaphore capping concurrent SEC requests at the 10/s ceiling). Each one:

1. Reads its slice of the fetched data
2. Does its analysis
3. Writes **Claims** to the Evidence Store — never free text

**Why parallel, and why this specific division of labor:** Running these sequentially would multiply latency for no benefit — they don't depend on each other. The division into five *distinct* agents (rather than one "analyst" agent asked to do everything) matters because each one gets a narrow, specific prompt with specific tools and a specific output schema. A single generalist agent tends to produce generic, hedge-everything prose. A Financial Agent that can *only* see financial data and *must* emit a structured `{revenue_growth, gross_margin, fcf_growth, ...}` object is forced to actually commit to numbers.

**The critical mechanic here — Claims, not text:** Every claim an agent makes has a `claim_type`: `evidence` (revenue grew 38% — directly observable in a filing), `inference` (this suggests demand is strong — the agent's reasoning from evidence), or `assumption` (growth continues at this pace — an unproven forward assumption). Any `evidence`-type claim is validated in Python to have at least one `evidence_id` pointing to a real row in the Evidence Store, and that row's quoted text is checked to appear *verbatim* in the actual stored source document — a plain string-containment check. If a claim fails this, the agent gets one retry with the validation error fed back; if it fails twice, the claim is dropped, not silently allowed through.

**Why this matters more than anything else in the project:** This is the actual anti-hallucination mechanism, and it's structural rather than a prompt asking the model to "please cite your sources." Prompted citation discipline degrades under pressure — an LLM asked to argue a bull case for a mediocre company will invent supporting facts if nothing stops it. A Python-level check that a claim's evidence ID resolves to real, verbatim text doesn't degrade. This is also the best answer to give in an interview when someone asks "how do you stop it hallucinating" — not "we told it not to," but "unsupported claims are structurally incapable of surviving validation."

**Tools:** BGE-M3 (a local embedding model, since Anthropic has no embeddings API and paid ones would violate the free-data constraint) plus pgvector's HNSW index and Postgres full-text search, combined via Reciprocal Rank Fusion, power the Filings Agent's retrieval. The reason for combining vector and keyword search rather than pure embeddings: SEC filings are full of near-identical boilerplate language across companies and years — "the Company faces significant competition" appears almost verbatim in thousands of 10-Ks — which is exactly the condition where semantic similarity stops discriminating well. A keyword/full-text signal catches exact terms (a specific customer name, a specific dollar figure, a specific regulation) that embeddings alone tend to miss.

**Model used:** Haiku for all five agents by default. Only escalate a specific agent to Sonnet if an evaluation run shows it's actually failing at Haiku's capability level — not on a hunch.

---

## Stage 4: Bull and Bear construct opposing cases

**What happens:** Two agents read *only* from the Evidence Store populated in Stage 3 — not fresh data, not each other's output yet. The Bull Agent's job is to build the strongest evidence-backed case for owning the stock; the Bear Agent, the strongest case against.

**The key structural decision:** These agents are not handed a research summary and told "argue for/against." They query the Evidence Store directly, and the same claim-validation rule from Stage 3 applies to them — every claim they make needs a citation. This is what prevents "debate theatre," where an LLM produces fluent-sounding argument regardless of whether the underlying company actually supports it. If a company has genuinely weak fundamentals, the Bull Agent should produce a *thin* case — three claims instead of ten — because there simply isn't enough cited evidence to build more. **A weak bull case is the system working correctly, not a bug to fix.**

**Model used:** Haiku is likely sufficient here too, since the agents are assembling and framing existing claims rather than synthesizing genuinely new insight — but this is a good candidate to actually eval against Sonnet before deciding, since argument quality is more subjective than extraction accuracy.

---

## Stage 5: The Critic attacks both sides, and the loop

**What happens:** The Critic Agent reads the Bull case, Bear case, and full evidence trail, and looks specifically for: claims stated with more confidence than their evidence supports, factual contradictions between Bull and Bear (not differences of interpretation — actual disagreement about a fact), and load-bearing assumptions that were never checked.

If it finds something significant — say, the Bull case assumes continued hyperscaler capex growth but no evidence agent actually investigated that — it emits a **targeted research request**: "find evidence on hyperscaler AI capex expectations." That request goes back to the relevant specialist agent (likely News or Competitive), which does focused additional research, and the new evidence flows back into the debate.

**Why this loop instead of a straight linear pipeline:** A linear pipeline (research → bull/bear → judge) never questions its own gaps. The loop is what makes this an actual multi-agent *debate* system rather than "five reports concatenated together and summarized." It's also the part of the architecture most worth explaining in an interview, because it demonstrates the system doing something a single LLM call fundamentally can't: identifying its own knowledge gap and going to fill it.

**Why it's capped at a small number of iterations (2–4):** Without a hard cap, a critic that always finds *something* to question could loop indefinitely, burning cost with no guarantee of convergence. The cap is a simple `if iteration >= max: proceed to judge anyway` check in the orchestration state.

**Model used:** Sonnet. This is the one place in the pipeline where I'd deliberately spend more — finding genuine contradictions and load-bearing gaps is harder than extraction, and getting it wrong (missing a real hole, or crying wolf on a non-issue) undermines the entire premise of the debate layer.

---

## Stage 6: Valuation runs alongside (not inside) the debate

**What happens:** Separately from the qualitative debate, `backend/calc/` runs a **reverse DCF** — instead of guessing a growth rate and computing a "fair value" (which is just projecting whatever assumptions you fed in), it takes the *current market price* as fixed and solves numerically for the growth rate the market must be implying to justify that price.

**Why reverse instead of forward:** A forward DCF asks an LLM (or you) to guess future growth and discount rate, then outputs a precise-looking number that's really just those guesses run through a formula — false precision dressed up as analysis. A reverse DCF turns the question around into something falsifiable: "the market is pricing in 22% growth for five years — is that plausible given this company's history and its market's total size?" That's a question the rest of the system (competitive analysis, industry growth data) is actually equipped to help answer. It also produces a number that's directly comparable across companies on a watchlist, since it's always "implied growth," not an idiosyncratic fair-value guess per company.

**Why this is 100% Python, zero LLM:** WACC, CAPM, the numerical solve for implied growth, the sensitivity grid across growth × discount rate — none of this should ever touch a language model. It's arithmetic. The LLM's only role anywhere near valuation is *interpreting* the output ("is 22% plausible?"), never computing it.

---

## Stage 7: Confidence is computed, not generated

**What happens:** Rather than asking Claude "how confident are you, 0 to 1," a Python function computes a confidence breakdown from measurable properties of the run: what fraction of high-materiality claims have citations, how recent the newest material evidence is, whether Bull and Bear contradicted each other on facts, how much required data was actually available versus missing.

**Why:** An LLM-generated confidence score is a number with no calibration behind it — nothing ties "78%" to any real-world frequency of being right. It's not reproducible (ask again, get a different number) and it's genuinely indefensible under questioning. A computed breakdown means "confidence is 71%" comes with an actual answer to "why 71%": here's the evidence coverage, here's the source recency, here's where Bull and Bear disagreed on facts.

---

## Stage 8: The Judge produces the final report

**What happens:** The Judge synthesizes everything — original research, both cases, the debate transcript, any additional evidence from the loop, the valuation output, the computed confidence — into the final structured report: recommendation, thesis, evidence, key risks, key catalysts, and critically, **thesis-invalidation conditions** — the specific, checkable things that would prove this wrong (e.g., "revenue growth falls below X% for two consecutive quarters").

**Why falsification conditions are a first-class output, not an afterthought:** Most stock-analysis output is unfalsifiable by design — "strong buy, bullish outlook" can't really be wrong in any checkable sense. Forcing the Judge to state specific conditions that would invalidate the thesis makes the whole exercise scientific rather than promotional, and it's a genuinely distinctive thing to point to in an interview: "the final agent has to produce falsifiable conditions, not just a directional call."

**Model used:** Sonnet — this is the highest-stakes synthesis step in the pipeline, pulling together the most context and requiring the most judgment.

---

## Tying it together: the four decisions that define the whole system

If you strip away everything else, four decisions are doing almost all the real work:

1. **`as_of` everywhere, from commit one** — makes the system honest about time and makes backtesting possible later instead of requiring a rewrite.
2. **Claims require evidence, enforced in code** — turns "don't hallucinate" from a prompt request into a structural guarantee.
3. **Numbers from XBRL, judgment from the LLM** — the dividing line that keeps arithmetic reliable and lets the LLM do what it's actually good at.
4. **Model tiering with an escalation rule, not a vibe** — Haiku everywhere by default, Sonnet only where an eval demonstrates the need, which keeps this affordable to actually run long-term rather than just to demo once.

Everything else in the architecture — the specific agents, the RAG mechanics, the debate loop — is really in service of making those four decisions actually hold up under real data.