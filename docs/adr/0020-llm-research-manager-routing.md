# ADR-0020: An LLM Research Manager that routes agents, with guardrails in code

## Status
Accepted — 2026-09-27

## Context
CLAUDE.md §7 lists "research planning and routing" as a Claude task, and
§15's Phase 5 is "Research Manager plans and routes." Increment 5a shipped
a deterministic plan instead: run every agent, skip Competitive when the
ticker has no curated peers, give Filings five fixed questions.

Two questions had to be decided:
1. **What should the Manager decide?** I recommended a hybrid: Python
   routes by preconditions, and Claude only writes the Filings questions.
   The project owner chose **fully LLM-routed**: Claude decides, for every
   agent, whether it runs.
2. **How do we keep an LLM planner safe?** The spec says retrieved content
   must never change the plan (§10), and a planner that can kill a run, or
   route to something that can't work, is worse than no planner.

## Decision
`backend/agents/manager.py` (`ResearchManager`) plans each run with Haiku.
It returns a `ResearchPlan`: for each agent, route with
a rationale or skip with a reason, plus 3–8 ticker-specific Filings
research questions.

The guardrails are enforced in code, not asked for in the prompt:
- **Closed choices.** Agent names are a `Literal`, so an invented agent
  fails schema validation.
- **Rules via `extra_validation`:**
  - every agent is routed or skipped exactly once
  - at least one agent is routed
  - Competitive only if the ticker is in the curated peer map
  - 3–8 non-empty Filings questions when Filings is routed (bounds in
    config)
  A violation gets the one retry with the error fed back
  (`call_structured`'s existing flow).
- **The planner can't kill a run.** Any Claude failure (two invalid
  outputs, a refusal, truncation, an API outage) or an SEC failure while
  fetching the company profile falls back to the deterministic plan,
  marked `source="fallback"` with a `fallback_reason`. The reason is
  streamed in `plan_ready`, printed by the CLI, and kept in state.
  `ResearchPlan` has a validator: a fallback must have a reason, and only
  a fallback may have one.
- **Trusted inputs only (C7, §10).** The Manager sees the ticker, as_of,
  the curated peer list, and SEC's own company name and SIC description
  (`backend/data/company.py`), wrapped in `<company_profile>` delimiters
  that the prompt declares data, never instructions. It never sees
  retrieved filing or news text, because planning happens before any
  retrieval. So injected content has no path to the plan.
- **As-of discipline in its one input.** The company name is resolved as of
  `as_of` from SEC's `formerNames` (a 2006 run sees "APPLE COMPUTER INC").
  SIC has no history in SEC's API, so it's today's classification, treated
  as time-invariant: a documented limitation.
- **Its cost counts.** The `plan` node gives the Manager a cost-tracked
  client, so the planning call appears in `llm_calls` like any agent's.

The `Planner` protocol (`backend/orchestration/planning.py`) is the seam:
real runs use `ResearchManager`, and tests use `DeterministicPlanner`.

## Alternatives considered
- **Hybrid: Python routes, Claude only writes questions.** Recommended.
  Fully reproducible routing, and the question-writing still exercises
  §7's "planning". Rejected by the owner in favour of full LLM routing.
- **Fully deterministic.** Cheapest and reproducible, but it leaves §7's
  planning role unbuilt. Kept as the fallback.
- **Fail the run when planning fails.** Rejected. A planner outage would
  then be a total outage, even though the deterministic plan is always
  available.
- **Silent fallback.** Rejected by CLAUDE.md §0.4 and C6. The fallback is
  visible in the plan, the stream, and the CLI output.

## Consequences
- **Non-determinism in which agents run.** This is the trade-off the owner
  accepted, and there is no knob to turn it down. The first version passed
  `temperature=0`, but the installed SDK (anthropic 1.6.0) has no sampling
  parameters on `messages.create` at all (no `temperature`, `top_p` or
  `seed`). The first live run failed with `TypeError: unexpected keyword
  argument 'temperature'`. The parameter was removed rather than smuggled
  in through `extra_body`, because this SDK version doesn't model it. So
  routing variance is whatever the model gives, and §12's consistency eval
  (three runs of the same ticker and as_of) is the only measure of it. It
  must report routing stability alongside recommendation stability.
  (CLAUDE.md §12 says to run that eval "at temperature 0", which this SDK
  can't do; that line needs updating.)
- **A skipped agent is a coverage gap.** Its reason is kept in
  `AgentOutcome.detail`, and Phase 7's `data_completeness` will read it.
  If routing turns out to skip useful agents, the evidence will be in
  those outcomes.
- **One more Haiku call per run**, small (a plan is well under 2,000
  output tokens). It's counted in the run's cost.
- **A contract test now guards every SDK call.** The `temperature` bug
  passed every test because a bare `MagicMock` accepts any keyword.
  `tests/conftest.py::assert_sdk_accepts_every_call` binds recorded
  `messages.create` kwargs against the real installed signature, and
  `tests/test_llm.py` applies it to a call with every optional part.
- **Revisit when the consistency eval exists:** if routing is unstable,
  constrain it further (for example, make Financial non-skippable) or move
  to the hybrid.
