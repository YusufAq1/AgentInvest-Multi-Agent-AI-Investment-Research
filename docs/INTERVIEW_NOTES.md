# Interview Notes

A running Q&A file, appended to as each component lands, so the design of
this system stays defensible cold. See `CLAUDE.md` §11.3.

## Why does every agent default to Haiku instead of Sonnet or Opus?

Because the workload — extraction, classification, routing, per-chunk
summarization — is squarely within what a small, cheap model handles well,
and this is a system meant to be run repeatedly, not demoed once. Defaulting
to a bigger model "to be safe" makes cost unpredictable for no measured
benefit. The rule: start every agent on Haiku, escalate only when an eval
shows it measurably fails a specific task, and record that evidence in an
ADR when it happens. Only the Critic, the Investment Judge, and final
narrative synthesis are expected to ever need Sonnet — because those tasks
involve finding genuine contradictions or synthesizing the highest-stakes
output in the pipeline, where a miss undermines the whole debate-layer
premise. See ADR-0008.

## Why uv instead of pip or Poetry?

One tool covers dependency resolution, virtualenv management, and running
scripts inside that environment, backed by a real lockfile (`uv.lock`) so
CI installs exactly what local development uses. pip needs `pip-tools`
bolted on to get a comparable lockfile; Poetry works but resolves
dependencies more slowly on non-trivial trees, which matters once
PyTorch-adjacent dependencies (BGE-M3, Phase 3) show up. See ADR-0010.

## How do you know a Claude call's cost is actually right?

`compute_cost()` in `backend/core/llm.py` is a pure function: token counts
in, a dollar amount out, using the per-model pricing table in
`Settings.model_pricing`. It's unit-tested against hand-worked values
(`tests/test_llm.py`) including the cache-read and cache-write terms, so
the arithmetic is verified independently of ever calling the real API. The
model never reports its own cost — Python computes it from the token counts
the API returns, the same principle (C4: numbers never come from an LLM)
that will later apply to every financial calculation in `backend/calc/`.

## How do you guarantee no look-ahead bias in data retrieval?

Every public method in `backend/data/` takes `as_of` and enforces it in
code — never by trusting an upstream API's own date filtering alone. Two
correctness details matter more than they look:

1. **XBRL facts are filtered on `filed`, never `end`/`fy`/`fp`.** Those
   fields describe the fiscal period a number reports on, not when it
   became public. A FY2023 10-K might be `filed` in early 2024 — filtering
   on the fiscal period instead of `filed` would leak a number that didn't
   exist yet at `as_of` into a point-in-time query. This is the exact bug
   class the project exists to prevent (C3).
2. **FRED needs both `observation_end` and `realtime_end`.** FRED tracks
   revision vintages — a GDP print gets revised months after first release.
   `observation_end` alone would let a later-revised value for an in-range
   period leak through; `realtime_end` is what actually excludes revisions
   published after `as_of`.

Every source module re-checks its own dates defensively in Python even when
the upstream API (or `edgartools`) claims to filter by date already — never
trust a third party's filtering without a local, testable check. The test
suite proves this directly: each source has a test that constructs a
fixture entry whose *period* looks in-range but whose *filed/publish* date
is after `as_of`, and asserts it's excluded — the data-layer's version of
§12's evidence leakage test, one phase earlier.

## Why does missing data return a value instead of raising?

C6 says a missing-data marker must "propagate into the report" — that's a
return-value contract, not an exception. If `get_company_facts()` raised on
every empty result, every future agent would need a `try/except` around
each of ~15 data calls across five sources, and forgetting one would crash
an entire research run instead of degrading one section of it.

But not every failure is treated this way. `backend/data` uses a hybrid:
*expected* misses (rate limit exhausted after retries, an unknown ticker,
no data in the requested window) are caught and returned as
`DataUnavailable`. A genuinely *unexpected* failure — a misconfigured EDGAR
identity, a response shape that no longer parses — is deliberately left to
propagate as a real exception. Converting everything to `DataUnavailable`
indiscriminately would let real bugs masquerade as ordinary missing-data
cases, which is worse than a loud crash during development.

## Why does yfinance have no fallback yet, when the spec calls for Stooq?

Because I checked, rather than trusting the spec: Stooq's plain CSV
endpoint is currently gated behind a JS anti-bot challenge and appears to
now require an API key with an unverified daily quota (confirmed live,
2026-09-16, corroborated by a GitHub issue from ~April 2026). Building a
fallback against a source I can't verify is durable would just move the
reliability problem, not solve it. Phase 1 ships yfinance-only, with
`DataUnavailable` on failure — an explicit, documented gap (ADR-0011), not
a silently downgraded feature.

## How do you prevent the LLM from fabricating a citation?

Two independent layers, not one. First, Claude never creates evidence — the
Financial Agent builds every `Evidence` row itself, deterministically, from
XBRL data, before Claude is ever called. Claude only *cites* `evidence_id`s
from the set it's shown; a hallucinated or out-of-set id fails
`EvidenceStore.resolve` (not a Pydantic concern — a bare `Claim` has no
store to check against), which triggers the same one-retry-then-drop
mechanism as a schema-level validation failure, and drops the whole batch
if the retry also fails. Second, independent of any given run, a
citation-validity sweep (`backend/evidence/validation.py`) re-verifies
every stored quote against its real source — substring containment for
prose and for XBRL JSON, recompute-and-compare against resolved input
evidence for derived ratios. That second layer is what catches a bug in
the agent's own quote-construction code, not just bad LLM behavior — the
test suite proves it directly by constructing a deliberately fabricated
quote and asserting it's rejected.

## Why does the Financial Agent call Claude at all if Python computes everything?

Because "which of these true, computed facts are worth stating, in what
words, and how material is each" is a judgment call, not arithmetic —
exactly CLAUDE.md §7's dividing line. Python owns every number; Claude
only decides what to say about numbers Python already computed and
verified, via a forced tool call it cannot deviate from into free text.
This also means the cheapest possible agent is the one that first exercises
the full structured-output + retry + drop machinery (`call_structured` in
`backend/core/llm.py`), so every later, more complex agent (Filings, Bull,
Bear, Critic) reuses it unchanged instead of it being designed in the
abstract for a hypothetical future need.

## Why is the Evidence Store in-memory in Phase 2?

Same reasoning as Phase 1's SQLite-not-Postgres cache: nothing in Phase 2
needs a claim or evidence row to outlive one process, so building
Postgres persistence now would be scope the exit criterion doesn't ask
for. It gets built for real (CLAUDE.md §14's `evidence`/`claims`/
`claim_evidence` tables) once a later phase actually needs a run's claims
to be queryable after the process exits — see ADR-0012.

## What actually broke the first time you ran the Financial Agent for real?

The unit tests all passed, but the first live run against AAPL produced a
78,267-input-token prompt — about 8 cents for one call, and Claude
truncated its response before finishing. The cause: `_build_fact_evidence`
turned *every* historical XBRL fact matching the concept aliases into
Evidence, and SEC companyfacts includes a concept's entire filing history —
Apple has been tagging `Revenues`/`CostOfRevenue`/etc. since ~2009, and
every subsequent 10-Q/10-K re-reports prior periods as comparatives, so one
concept easily has 50+ entries. All of that was getting formatted into the
prompt, even though only the current and one prior period are ever
actually used.

The fix wasn't a bigger `max_tokens` — that treats the symptom. It was
making evidence construction lazy: `run()` now only calls
`ensure_fact_evidence(fact)` for the specific facts the ratio-selection
logic (`_select_anchor`/`_select_matching`/`_select_prior_year`) actually
picked, closing over a memoizing dict instead of eagerly building Evidence
for the whole candidate pool up front. A run now creates on the order of
10 evidence rows (the handful of facts used, plus their computed ratios)
regardless of how many years of history a filer has. There's a regression
test for this specifically (`test_run_does_not_turn_entire_history_into_evidence`)
that constructs 15 years of fake revenue history and asserts the evidence
count stays bounded — this is exactly the kind of bug unit tests with
small, hand-built fixtures don't catch on their own, because the fixtures
never had enough history to reproduce the blowup. It's why the phase
scripts (`hello_world.py`, `data_layer_demo.py`, `financial_agent_demo.py`)
exist as a real end-to-end check alongside the test suite, not a
redundant formality.

## Why hybrid search (pgvector + full-text) instead of pure embeddings?

Dense embeddings and keyword search fail in complementary ways. A query
containing an exact ticker, dollar figure, or product name can score worse
by cosine similarity than a vaguer, topically-related passage, because an
embedding model compresses exact lexical detail into the same space as
paraphrase — full-text search catches that trivially. The reverse also
holds: a paraphrased query with no shared vocabulary scores zero on full-
text search but is exactly what embeddings are built for. Fusing two
ranked lists via Reciprocal Rank Fusion (`backend/rag/retrieval.py`,
ADR-0014) sidesteps the harder problem of combining them into one query:
cosine distance and Postgres `ts_rank` live on incompatible scales with no
principled shared unit, so RRF only uses each list's *rank*, not its raw
score, avoiding an arbitrary blending weight that couldn't be justified
either way. `tests/rag/test_retrieval.py` proves the complementary-
failure-mode claim directly, not just in theory: one seeded chunk is only
findable via an exact keyword match with a deliberately orthogonal
embedding, another is only findable via a matching embedding with zero
shared vocabulary, and both surface correctly.

## How do you validate section boundaries you didn't compute yourself?

The honest answer involves a failed first attempt, which is a more useful
interview answer than a clean success story. The original design trusted
edgartools' `Section.start_offset`/`end_offset` when its own
`confidence`/`detection_method` looked reliable, and otherwise fell back
to locating the section's text via substring search inside the filing's
separately-fetched full text — the same never-trust-upstream-blindly
principle every `as_of` recheck in `backend/data` already applies, just
extended to a new kind of claim.

Running that against a real AAPL 10-K broke it completely: every
section's `start_offset` was `0` (not usable global offsets at all), and
the substring-search fallback also failed for every section, because
edgartools extracts a section's text and the whole document's text
through different internal code paths that turn out not to be
byte-comparable, even when the underlying content is genuinely present in
both. A test suite built only from small, hand-crafted fixtures never
would have caught this — the fixture author controls both strings, so the
search trivially succeeds in a way it never did against real data.

The fix wasn't a smarter search — it was not needing one. `chunking.py`
now reconstructs the filing's stored text FROM the sections
(`build_full_text()`, concatenating each section's own text), so a
chunk's char offset is computed while building that text, not searched
for inside a separately-obtained one. There's no trust threshold and no
failure mode left at the offset level — `confidence`/`detection_method`
are still used, but to decide whether to bother chunking a section at all
(skip ones edgartools itself flags as unreliably detected), not to decide
whether to believe a location. Re-verified against the same real filing
after the fix: 110 chunks, zero offset mismatches. See ADR-0015's revision
history for the full account.

## Why recall@5 instead of a generation-quality metric for this phase?

Phase 3 deliberately stops at retrieval — nothing in it wires RAG into an
agent yet (that's Phase 4's Filings Agent). Measuring recall@5 (does the
labelled correct section appear in the top 5 results?) isolates whether
the *retrieval* layer works at all, independent of anything an LLM might
later do with what it retrieves. Combining retrieval and generation into
one metric this early would make a retrieval bug and a generation bug
indistinguishable from the same failing number.

## Why Supabase instead of the docker-compose Postgres CLAUDE.md specifies?

An honest discrepancy, not a silent one: Docker Desktop wasn't running in
this project's dev environment when Phase 3 started, and rather than block
all database-touching work on fixing that, a Supabase project (free tier,
pgvector enabled by default) was used instead — reached via a plain direct
Postgres connection (SQLAlchemy + asyncpg), not Supabase's REST client,
since hybrid retrieval needs raw SQL (HNSW-ordered queries, `tsvector`
predicates) that a REST layer doesn't expose cleanly. `docker-compose.yml`
stays in the repo unchanged as a self-host fallback, and CI's test
database is still the same `pgvector/pgvector:pg16` image it always
was — only local day-to-day dev points at a different host. See ADR-0013.

## Why can't the Competitive Agent state a market-share number?

Structurally, not just by instruction: no data source in this project
(there's no free market-share/TAM API — CLAUDE.md's own data-source table
has no such row) can produce that figure, so Python never creates a
market-share `Evidence` row, so Claude can never cite one, and
`EvidenceStore.validate_claim_batch` rejects any `claim_type="evidence"`
attempt at one the same way it rejects any hallucinated citation — the
identical mechanism that makes every other agent's claims impossible to
fabricate, not a special case built for this rule.

There's one gap that mechanism doesn't close on its own: an
`"inference"`/`"assumption"` claim can cite *real* revenue evidence while
asserting an unrelated market-share conclusion (e.g. "revenue is 3x
theirs, so we hold 60% of the market") — those claim types aren't checked
for whether their cited evidence actually supports the specific thing
being asserted, by design (CLAUDE.md §6 rule 5 treats them as judgement,
not verifiable fact). That gap is closed by an explicit prompt
instruction instead, which is honestly weaker than a structural
guarantee — worth watching in real runs, not assumed solved. See
ADR-0018.

## Why is the News Agent's citation a formatted string instead of the actual 8-K text?

Because `NewsClient` (Phase 1) never fetches an 8-K's narrative body —
only structured item-code metadata (accession, filing date, item codes,
their SEC labels), a deliberate scoping decision documented in its own
module docstring, not an oversight this phase quietly inherited. The News
Agent's `Evidence.quote` is a deterministic string built only from those
real fields — `"8-K filed {date} (accession {accession}): 1.03
(Bankruptcy or Receivership)"` — the same category of construction as a
`"computed"` ratio's `f"{name} = {formula} = {value}"` string, never
freeform prose Claude could have written itself.

The honest cost: this makes the citation-validity containment check
tautological for `news` evidence specifically — the "document" checked
against is the same string as the quote, since there's nothing separately
fetched to check it against. I could have hidden that, but the real
anti-fabrication guarantee here (as everywhere else) is structural —
Claude only cites an `evidence_id`, it never writes a quote — so saying
plainly that the containment check adds nothing extra for this one source
type is more honest than implying a symmetry with `sec_filing` evidence's
check that doesn't actually carry the same weight. See ADR-0017.

## How do you prove agents actually run in parallel, given a shared, mutable Evidence Store?

Two layers. First, a real bug had to be found and fixed before the claim
was even true: `ClaudeClient.call_structured` wraps a synchronous
Anthropic client, and the Financial Agent called it directly inside
`async def run()` — invisible with one agent, but it would have silently
serialized every "concurrent" agent's Claude call behind whichever ran
first under `asyncio.gather`. Wrapping the call in `asyncio.to_thread`
across all four agents actually makes the concurrency real (ADR-0019).

Second, `tests/agents/test_parallel_execution.py` is the automated proof:
all four agents share one `EvidenceStore` instance, run concurrently via
`asyncio.gather`, and the test asserts every claim/evidence row lands
correctly — nothing lost, nothing duplicated, nothing cross-attributed to
the wrong agent. This works safely with no new locking code because
`EvidenceStore`'s methods are fully synchronous between `await` points —
Python's cooperative scheduling guarantees one coroutine's method call
completes before another's begins, so a shared mutable dict-backed store
is safe under concurrent async use by construction, not by careful manual
synchronization. Worth being precise about what the test proves versus
what it doesn't: the correctness assertions would have passed even before
the `asyncio.to_thread` fix (nothing about them depends on genuine
overlap) — proving genuine wall-clock overlap is what
`scripts/phase4_agents_demo.py`'s timing print is for instead.

(Phase 5 update: the shared store is gone. Each agent node now has its own
store, and the outputs merge through LangGraph reducers. See the next
questions and ADR-0001.)

## Why LangGraph, if your graph is only plan → four parallel agents → collect?

Honestly, the graph alone doesn't justify it, and the ADR says so. My own
spec set the bar at more than about three conditional branches or a need
for resumable state, and Phase 5 has one branch and no loops. I adopted it
early for three reasons.
- Phase 7's critic loop is a conditional back-edge that crosses the bar
  anyway, and I'd rather not migrate while building the debate layer.
- `astream` is exactly the progress stream Phase 8's SSE endpoint needs.
- Checkpointing gives resumable runs for free.

The real cost was design, not code: checkpointed state must be
serialisable. So the shared `EvidenceStore` became per-agent stores whose
output merges through reducers, plus a fan-in node that re-validates
everything. I also took on a second Postgres driver, psycopg, because the
checkpointer requires it (ADR-0001).

## What happens if one agent crashes mid-run?

That agent's node catches the exception and records an `AgentOutcome` with `status="failed"` and the
error. The other agents finish normally. The failed agent contributes no
claims or evidence, because half an agent's output is harder to reason
about than none. But the Claude cost it already spent is kept, because
that money was really spent. The failure isn't hidden: it's in the outcome
table the CLI prints, and it's what Phase 7's `data_completeness` score
will read. Ctrl-C is different: it's a `BaseException`, so it propagates
and the run can be resumed.

Broad `except Exception` exists in only three kinds of place, each with a
WHY comment: the edgartools and yfinance boundaries in the data layer
(third-party libraries with no documented exception contract), and this
agent bulkhead. Everywhere else catches a named exception type.

## How do you know what one research run cost?

Every Claude call goes through `ClaudeClient`, which prices it from a
config table. Each agent node gets its own client with an `on_result`
callback that appends the call's `LLMCallResult` to the node's state
update. A run's cost is `total_cost(state["llm_calls"])`, a pure sum,
printed at the end of every run. Before Phase 5, the demos scraped cost
out of log records with a global handler, which would have mixed two
concurrent runs together. One known under-count: a call made inside a node
that gets interrupted before returning is paid but never checkpointed. It's
still in the JSON log, and a per-call `llm_calls` table would close the gap.

## How is a run resumed after a crash?

LangGraph checkpoints the state to Postgres after every superstep, keyed by
`thread_id`, which I set to the `run_id`. It also records each finished
node's result within a superstep. `run_research.py --resume <run_id>`
rebuilds the graph with fresh clients and continues that thread. Agents
that already finished aren't rerun or re-paid; only the interrupted ones
run again. A test proves this by interrupting one of four parallel agents
and asserting the other three were built exactly once across both
attempts.

## How does the manager decide which agents to run?

Haiku decides. For every agent it either routes it with a
rationale or skips it with a reason, and it writes 3–8 research questions
for the Filings Agent tailored to the kind of business. But what it's
allowed to say is enforced in code, not in the prompt.
- Agent names are a closed `Literal`.
- Every agent must be decided exactly once, and at least one must run.
- Competitive can only be routed if the ticker has a curated peer group.
- The question count is bounded.

A violation gets one retry with the error fed back. A second failure, a
refusal, an API outage, or SEC being down all fall back to a
deterministic plan, which is marked `fallback` with the reason and printed.
The planner can never be what kills a run. The trade-off I accepted is
some non-determinism in routing. Temperature 0 minimises it, and the
consistency eval will measure it (ADR-0020).

## Can a prompt injection change the research plan?

Not through retrieved content, because planning runs before any retrieval.
The Manager sees only the ticker, the date, our curated peer list, and SEC's
own name and industry code, wrapped in delimiters the prompt declares to
be data. Even if that metadata carried an instruction, the output still
has to pass the code-level rules above, so the worst an injection could do
is produce a *valid* plan, which is equally a plan the model might choose
anyway. There's a test that plants an instruction in the company name and
checks it stays inside the delimiters and the plan still validates.

## Your tests passed but the first live run crashed. What happened?

The Research Manager passed `temperature=0` to make routing more
repeatable. The installed Anthropic SDK has no sampling parameters on
`messages.create` at all, so the real call failed with a `TypeError`. Every
test passed because they mocked the SDK with a bare `MagicMock`, which
accepts any keyword. I had tested my code against my assumption about the
SDK, not against the SDK.

Two fixes. I removed the parameter; I didn't force it through `extra_body`,
because that would quietly send something this SDK version doesn't model.
And I added a contract test: every mocked `messages.create` call is bound
against the real installed signature with `inspect.signature(...).bind`, so
an unknown keyword now fails in CI instead of in a live run.

The crash was also the correct behaviour. The Manager falls back to the
deterministic plan on *expected* failures: bad output, a refusal, an API
outage, SEC being down. A `TypeError` is a bug in my code. Falling back on
it would have hidden the bug behind a plausible-looking run, so it's right
that it stopped the run before any agent spent anything.

## Why a reverse DCF instead of a normal DCF?

A normal DCF asks me to guess the growth rate, the margins and the
discount rate, then prints a "fair value" to two decimals. That number is
just my guesses with a dollar sign on them. A reverse DCF takes the market
price as given and solves for the growth the price implies. "The market is
pricing in 9% a year for ten years" is a claim I can actually test against
the company's own history, and testing claims is what the rest of the
system is built for. That's Rappaport and Mauboussin's *Expectations
Investing*. I still show a growth × WACC sensitivity grid, so the reader
sees how much the answer moves with the discount rate instead of trusting
one number.

## Doesn't higher growth always mean a higher valuation?

No, and building the solver taught me that. In my model, growth has to be
paid for: every extra dollar of revenue needs some investment. Growth only
adds value if the return on that investment beats the cost of capital.
The break-even investment rate works out to margin × (1 − tax) × (1 +
WACC) / WACC.
- **Below break-even**, enterprise value rises with growth.
- **Above it**, value *falls* as growth rises, so a higher stock price
  actually implies lower growth.
- **Exactly at it**, growth doesn't change value at all, and the price
  tells you nothing about growth.

I first assumed the curve could rise and then fall and designed for
multiple answers. When I tested it, it's always monotonic for constant
inputs, so I replaced that with a direction flag, an explicit "growth not
identifiable" result, and a hard error if the solver ever finds two
answers. A property test across 48 combinations pins it down.

## How do you stop the LLM getting the maths wrong?

The LLM never sees the maths. Cost of equity, WACC, beta, the DCF, the
solver, CAGR and the sensitivity grid are all pure Python functions in
`backend/calc/`, with tests whose expected values I worked out by hand in
the test docstrings. One test walks a two-year DCF line by line: revenue,
NOPAT, investment, free cash flow, discounting, terminal value. Another
prices the model at a known growth rate and checks the solver returns
exactly that rate. Every result carries its formula and inputs, so when it
becomes evidence the citation check can recompute it from scratch. Claude
only interprets the numbers; it can't change them.

## Where else did look-ahead bias hide, beyond filings?

In the stock price. yfinance's history is adjusted for every split and
dividend up to today. So NVDA's close on 2024-05-31 comes back as about
$109, when it actually traded at $1,096; the 10-for-1 split happened ten
days *later*. Multiply that by the share count from Nvidia's own filing and
the market cap is ten times too small. The series knows about a corporate
action that hadn't happened yet.

I fixed it by fetching dividend-unadjusted prices and multiplying back by
the splits after each date. That recovers the price that actually printed.
Using later splits to *undo* the vendor's adjustment isn't look-ahead,
because it only recovers a number that was public that day.

There was a second, subtler problem. The share count is reported weeks
before the price date, so across a split it's on the wrong basis even with
correct prices. So the count is restated for splits between its date and
the price date, both of which are before as_of. The adjusted series is
still right for beta, because returns should include dividends. I verified
both NVDA market caps against reality: $2.70T before the split, $3.04T
after.

## How did you verify the valuation is right?

Three ways.
- Every formula has unit tests with hand-worked expected values.
- I re-derived a real run by hand. `docs/valuation_worked_example.md` takes
  AAPL as of mid-2024 and redoes every step from the printed inputs, with
  plain arithmetic that doesn't import my code: margins, working capital,
  the investment rate, WACC, and the ten-year cash-flow table. It lands on
  18.555% implied growth, against the module's 18.56%.
- I checked against known facts: AAPL's 2024-06-28 close of $210.62, and
  NVDA's market cap on both sides of its split.

Building it also surfaced a Phase 1 bug. My FRED client had never actually
worked for daily series. It sent only `realtime_end`, so FRED assumed a
start date in 1776 and rejected the request for having too many vintages.
The tests mocked FRED, so they never saw it. The fix pins both ends to
as_of, which is exactly the point-in-time question anyway.

## How do you stop the LLM from misquoting a valuation number?

Claude never produces one. Python computes the whole chain: margin,
reinvestment, market cap, net debt, beta, cost of equity, WACC, then the
implied growth. Every number is stored as evidence that cites the rows it
came from. Some inputs are sums or differences of rows, like three years of
capex or revenue now minus revenue three years ago, so citations can be
signed combinations of rows. CI then recomputes every number from what it
cites and fails on any mismatch. That includes re-running the growth solver.

Claude sees about 15 headline rows and may only compare them in words. It
can't do arithmetic or make up a number. It cites a row's id, and the
validator rejects any id that doesn't exist. If someone edits the stored
margin, two checks fail: the margin's own recompute, and the implied
growth's, because that row recomputes from the margin row.

## What does "the market implies 18.6% growth" actually mean, and when is there no answer?

It means: hold this company's margin, reinvestment and cost of capital
fixed, and the current share price only makes sense if revenue grows about
18.6% a year for ten years. It's a statement about the market's
expectations, not a price target. Comparing it with the company's own
history (11.8% over three years for Apple in mid-2024) is where the
research starts.

There are three ways to get no clean answer:
- **The price is outside what any growth in the search range can
  produce.** The system says so and names the bound, rather than clipping
  to it.
- **Reinvestment sits exactly at break-even.** Growth then adds no value,
  so the price can't tell you anything about growth.
- **Required data is missing.** Ford tags no debt in XBRL, for example.
  The agent then fails visibly and lists every missing input, rather than
  returning nothing quietly.
