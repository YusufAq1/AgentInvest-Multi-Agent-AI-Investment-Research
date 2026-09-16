"""Phase 0 exit criterion: make one real Claude call through ClaudeClient
and prove the cost-tracking pipeline works end-to-end by printing what it
logged.

Run with: uv run python scripts/hello_world.py

WHY this lives in scripts/, not backend/: it's a one-off operational entry
point, not part of the importable application package. CLAUDE.md §13's
repository layout doesn't show a scripts/ directory — this is a genuine gap
in the spec, since Phase 0's exit criterion requires a hello-world script to
exist somewhere. Repo-root scripts/ is the conventional place for it.
"""

from backend.core.config import Settings
from backend.core.llm import ClaudeClient
from backend.core.logging import configure_logging


def main() -> None:
    # WHY the ignore: Settings' required fields have no Python-level default
    # because they're meant to come from .env at runtime (pydantic-settings'
    # whole point). mypy's pydantic support doesn't know that and expects
    # them as constructor args — a known, documented friction between
    # BaseSettings and static analysis.
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)

    client = ClaudeClient(settings)
    result = client.call(
        agent="phase0_hello_world",
        system="You are a terse assistant.",
        messages=[
            {
                "role": "user",
                "content": (
                    "Say hello, and name one thing a multi-agent research "
                    "system can do that a single LLM call can't."
                ),
            }
        ],
        max_tokens=256,
    )

    print(result.text)
    print()
    print(f"=== Claude call: {result.agent} ===")
    print(f"Model: {result.model}")
    print(
        f"Input tokens:  {result.input_tokens}   "
        f"(cache read: {result.cache_read_input_tokens}, "
        f"cache write: {result.cache_creation_input_tokens})"
    )
    print(f"Output tokens: {result.output_tokens}")
    print(f"Latency: {result.latency_ms:.0f} ms")
    print(f"Cost: ${result.cost_usd:.6f}")
    print()
    print("=== Run total ===")
    print(f"Total Claude cost this run: ${result.cost_usd:.6f}")


if __name__ == "__main__":
    main()
