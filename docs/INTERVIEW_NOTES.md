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
