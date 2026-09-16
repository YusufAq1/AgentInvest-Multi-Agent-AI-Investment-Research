# ADR-0008: Haiku-by-default model tiering

## Status
Accepted — 2026-09-15

## Context
AgentInvest's only paid dependency is the Anthropic API (C1), and this is a
system meant to be run repeatedly over time, not demoed once (C2). Every
agent in the pipeline needs a model assigned to it, and left unconstrained,
it's easy to default everything to the most capable model "to be safe,"
which makes per-run cost unpredictable and, at scale, expensive for no
measured benefit.

## Decision
Every agent defaults to **Claude Haiku 4.5** (`claude-haiku-4-5-20251001`)
unless an evaluation run demonstrates it measurably fails at a specific
task — not "it felt better." Model choice and pricing are set in
`backend/core/config.py`, never hardcoded at an individual call site, so
escalating one agent never requires touching call-site code.

The only agents expected to ever run on Sonnet are the Critic, the
Investment Judge, and final narrative synthesis — tasks that involve
finding genuine contradictions and load-bearing gaps, or synthesizing the
highest-stakes output in the pipeline, where the cost of a Haiku model
missing something meaningfully undermines the whole debate-layer premise.
Opus and Fable-tier models are out of scope entirely — not justified for
this workload's task complexity.

Pricing (verified against Anthropic's published rates, per-Mtok, as of
2026-09-15):

| Model | Input | Output | Cache write (5m) | Cache write (1h) | Cache read |
|---|---|---|---|---|---|
| Claude Haiku 4.5 | $1.00 | $5.00 | $1.25 | $2.00 | $0.10 |
| Claude Sonnet 5 | $2.00 | $10.00 | $2.50 | $4.00 | $0.20 |

**Correction from the original spec:** CLAUDE.md §5 estimated Sonnet 5 at
"~$3 / $15 (verify)". The verified published rate is $2.00 / $10.00 per
Mtok. Pricing lives entirely in `Settings.model_pricing`
(`backend/core/config.py`), so correcting this required no code change
anywhere else — which is the point of keeping it there instead of inline
at call sites.

Prompt caching is mandatory for system prompts, tool definitions, and the
shared research package: cache reads bill at 0.1x base input price, cache
writes at 1.25x (5-minute TTL, the default) or 2x (1-hour TTL). Haiku 4.5's
minimum cacheable prefix is 4,096 tokens — below that, `cache_control` is
silently ignored and the full price is paid with no error. Every Claude
call is logged (agent, model, tokens in/out, cache hits, latency, estimated
cost) via `backend/core/llm.py`, and a per-run cost total is printed at the
end of every run.

## Alternatives considered
- **Default everything to Sonnet or Opus "to be safe."** Rejected: directly
  violates C2, and there is no evidence yet that any Phase 0/1 task needs
  more capability than Haiku provides — extraction, classification, and
  routing are squarely in Haiku's documented strength.
- **Pick a model per-agent by intuition rather than eval.** Rejected: "it
  felt better" is explicitly disallowed by the working agreement (§0.3) —
  every escalation must cite the eval evidence that justified it.
- **Hardcode model strings at each call site.** Rejected: makes future
  escalation (or a pricing/model-ID change, as happened with the Sonnet
  correction above) a multi-file find-and-replace instead of a one-line
  config edit.

## Consequences
Easy: adding a new agent means picking a default (Haiku, per this ADR) and
confirming its pricing entry exists — both in one file. Cost is visible
per-call and per-run from the first commit, not retrofitted later.

Hard: this ADR's pricing table is a snapshot and will drift as Anthropic
updates rates — revisit `config.py`'s `model_pricing` whenever that
happens. Revisit this ADR itself whenever an agent is actually escalated to
Sonnet: record the eval evidence that justified it here or in a follow-up
ADR, per the escalation rule above.
