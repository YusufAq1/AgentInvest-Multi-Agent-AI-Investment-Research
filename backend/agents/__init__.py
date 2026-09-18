"""Research agents. Each reads from backend/data/, writes to an
EvidenceStore, and (if it uses Claude at all) does so only through
ClaudeClient.call_structured — never free text for anything that becomes a
Claim (CLAUDE.md §6).
"""
