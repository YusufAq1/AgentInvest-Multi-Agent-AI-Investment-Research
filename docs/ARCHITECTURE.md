# Architecture

AgentInvest takes a ticker and an as-of date and produces an auditable,
evidence-linked equity research report. The intended end-state pipeline
(see `Overview.md` for the full narrative walkthrough):

```
(ticker, as_of) -> Research Manager -> specialist agents (parallel)
                 -> Evidence Store (citation-enforced claims)
                 -> Bull / Bear agents (read-only over Evidence Store)
                 -> Critic (flags unsupported claims, can trigger more research)
                 -> Investment Judge -> Research Report (thesis, evidence,
                    falsification conditions, computed confidence)
```

The two structural rules that make this different from a single prompted
LLM call:

1. **Every material claim needs a citation that resolves to stored source
   text**, checked in Python (string containment against the original
   document), not just requested in a prompt.
2. **Numbers never come from an LLM.** All arithmetic — ratios, growth
   rates, DCF, confidence scores — is a pure, unit-tested Python function.
   The LLM interprets results; it never computes them.

## Current status: Phase 0 — Foundation

Implemented so far:
- `backend/core/config.py` — process settings (Anthropic key, default
  model, per-model pricing table, Postgres connection).
- `backend/core/logging.py` — structured (JSON-lines) logging setup.
- `backend/core/llm.py` — `ClaudeClient`, a thin wrapper around the
  Anthropic SDK that computes and logs the cost of every call.
- `scripts/hello_world.py` — proves the above works end-to-end with one
  real Claude call.
- Docker Compose brings up Postgres + pgvector, but nothing reads or
  writes to it yet.

Not yet implemented: the data layer, the Evidence Store, any research
agent, retrieval, valuation, the debate layer, the API, or the frontend.
This file will grow with each phase — see `CLAUDE.md` §15 for the phase
plan and §11 for the documentation standard this file follows.
