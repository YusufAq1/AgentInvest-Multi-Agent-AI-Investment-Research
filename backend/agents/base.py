"""The shared agent contract (CLAUDE.md §13).

WHY a Protocol, not an ABC: no agent shares implementation yet — the
Financial Agent's constructor (XBRLClient + ClaudeClient + EvidenceStore +
Settings) is already known to differ from what a Filings/News/Competitive
agent will need (different data clients, possibly no ratio computation at
all). A Protocol enforces the shape every agent must have without forcing a
common inheritance chain none of them actually share yet. Revisit once a
second agent reveals real shared behavior worth an actual base class.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from backend.evidence.models import Claim


class ResearchAgent(Protocol):
    @property
    def agent_name(self) -> str: ...

    async def run(self, ticker: str, as_of: date) -> list[Claim]: ...
