"""Loads agent prompts from backend/agents/prompts/.

WHY prompts live in versioned files, not inline in functions (CLAUDE.md
§16): a prompt is a real, diffable artifact — filename-versioned (`_v1`,
`_v2`, ...) rather than relying on git history alone, so a future eval run
can pin/compare a literal prompt version without git-blame archaeology.
"""

from __future__ import annotations

from pathlib import Path

from backend.core.llm import AgentInvestError

_PROMPTS_DIR = Path(__file__).parent / "prompts"


class PromptNotFoundError(AgentInvestError):
    """No prompt file exists for the given name."""


def load_prompt(name: str) -> str:
    """`name` is the filename without its `.md` extension, e.g.
    `"financial_agent_v1"`."""
    path = _PROMPTS_DIR / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise PromptNotFoundError(f"No prompt file found at {path}") from None
