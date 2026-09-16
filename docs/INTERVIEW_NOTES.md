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
