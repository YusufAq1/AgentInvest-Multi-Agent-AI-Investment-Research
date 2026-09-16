# ADR-0010: Repo tooling — uv for dependency management

## Status
Accepted — 2026-09-15

## Context
The project needs a Python dependency/environment manager from the first
commit. The working agreement (§0.3) requires any library or tooling
choice to be justified in the ADR log, not picked silently — and a
dependency manager is a foundational choice that's expensive to change
later, so it's worth deciding deliberately rather than defaulting to
whatever's most familiar.

## Decision
Use **uv** (Astral) for dependency resolution, virtual environment
management, and script execution, with `pyproject.toml` plus a committed
`uv.lock` as the single source of truth for dependencies.

- `uv sync` installs the exact locked environment.
- `uv run <cmd>` runs anything inside that environment without manual venv
  activation.
- `uv add` / `uv add --dev <pkg>` add a dependency and update the lockfile
  in one step.
- CI installs via `astral-sh/setup-uv` and runs `uv sync --locked`, so a
  lockfile that's drifted from `pyproject.toml` fails the build instead of
  silently resolving different versions than local development used.

## Alternatives considered
- **pip + `requirements.txt`.** Rejected: no lockfile with hashes by
  default (would need `pip-tools` bolted on to get one), no built-in
  virtualenv or Python-version management, and "install," "add a
  dependency," and "run a script in the environment" are three separate
  steps instead of one tool covering all three.
- **Poetry.** Rejected: historically much slower dependency resolution on
  non-trivial dependency trees (relevant once BGE-M3/PyTorch-adjacent
  dependencies show up from Phase 3 onward), a separate non-standard
  `[tool.poetry]` metadata table rather than the PEP 621 `[project]` table
  uv reads directly, and no meaningful advantage over uv for this
  project's needs.
- **conda.** Rejected: overkill for this dependency set. Nothing in Phase
  0-2 needs conda's native/binary package management, and it introduces an
  entirely separate environment model to reason about for no benefit yet.

## Consequences
Easy: fast, reproducible installs from a single lockfile; no manual venv
activation (`uv run pytest`, `uv run python scripts/hello_world.py`);
adding a dependency is one command that also updates the lock.

Hard: uv is newer and less universally known than pip — anyone else
touching this repo needs `uv` installed rather than assuming a bare
`pip install -r requirements.txt` works. Revisit if a future dependency
(e.g. a PyTorch build for BGE-M3 in Phase 3) needs a custom index URL or
platform-specific wheel selection uv can't express cleanly via
`[tool.uv.sources]` — not expected today, but the one place this choice
could get friction later.
