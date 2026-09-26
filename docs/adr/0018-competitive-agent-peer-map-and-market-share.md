# ADR-0018: Hand-curated peer map, and prompt-only market-share enforcement

## Status
Accepted — 2026-09-22

## Context
CLAUDE.md §15's Phase 4 calls for a "Competitive Agent with peers from a
curated map, and an explicit rule that market-share claims require a
citation or are not made." Two design questions this raised: where do
peer groups come from, given no free structured peer/competitor API
exists in this project's stack (confirmed: CLAUDE.md §4's data-source
table has no such row); and what enforces the market-share rule.

## Decision

**1. A hand-curated, starter `PEER_MAP`** (`backend/agents/peer_map.py`),
covering five well-known large caps (AAPL, MSFT, GOOGL, AMZN, META), each
mapped to 2-3 peers chosen by business-model overlap. SIC-code-based peer
discovery was considered and rejected: SIC codes are decades-old, broad
industry buckets that misclassify modern large-caps — Apple's own SIC
code (3663, "Radio & TV Broadcasting & Communications Equipment")
reflects a 1980s-era taxonomy, nothing like what Apple actually competes
in today. CLAUDE.md's own "curated map" wording anticipates hand curation
being the more accurate choice at this scale, the same tradeoff already
made for Phase 3's 15-question retrieval eval starter set.

**2. Market-share enforcement needs zero new validation code — it
inherits the Evidence Store's existing structural guarantee.** No
`backend/data/` module can produce market-share or total-addressable
-market data, so Python never creates such an `Evidence` row, so Claude
can never cite one, so `EvidenceStore.validate_claim_batch` structurally
rejects any `claim_type="evidence"` attempt at a market-share figure —
the same mechanism that already makes every other agent's citations
impossible to fabricate.

**3. One gap that enforcement doesn't structurally close, closed by an
explicit prompt instruction instead:** an `"inference"`/`"assumption"`
claim can cite *real* revenue evidence while asserting an unrelated
market-share number (e.g. "Company X's revenue is 3x Company Y's, so it
holds roughly 60% of the market") — `inference`/`assumption` claims are
not checked for whether their cited evidence actually supports the
specific assertion made, per CLAUDE.md §6 rule 5's "judgement, not fact"
category (that check would need to understand the *semantic content* of
an inference, not just that its evidence_ids resolve — a materially
different, much harder validation problem, out of scope here).
`competitive_agent_v1.md` closes exactly this gap with an explicit
instruction: revenue-scale comparisons are fine as inferences; market
-share conclusions drawn from revenue-only evidence are not, because
revenue scale and market share are different, unmeasured quantities.

## Alternatives considered
- **SIC-code-based peer lookup** (available in EDGAR's `submissions.json`
  as a company's `sic` field, free and already reachable). Rejected: see
  the AAPL example above — SIC proximity is a poor proxy for actual
  competitive grouping among modern large-caps, and CLAUDE.md's Phase 4
  wording explicitly calls for "a curated map," not an automated lookup.
- **A separate, stricter `claim_type` or validation path just for
  Competitive Agent claims, checking cited evidence's semantic relevance
  to the stated claim.** Rejected as significant, speculative complexity:
  no other agent needs semantic-relevance checking, and the actual gap
  (inference claims conflating measured and unmeasured quantities) is
  adequately closed by an explicit, cheap prompt instruction — the
  Financial and Filings Agents' prompts already lean on the same kind of
  instruction-level guidance for `claim_type` selection.
- **Reuse `backend/agents/financial.py`'s Evidence-construction methods
  directly for the Competitive Agent.** Rejected — see ADR-0016: only the
  XBRL *selection* logic (in `backend/agents/xbrl_facts.py`) is shared;
  Competitive Agent's Evidence shape differs enough (fewer ratios,
  multi-company, `location["company"]`-tagged) that sharing construction
  code would need more parameterization than it would save.

## Consequences
Easy: extending peer coverage to a new ticker is a one-line addition to
`PEER_MAP`, reviewable independently of any code change. The market-share
rule required no new Evidence Store, validation, or Claim-model code —
direct evidence it was already structurally sound before this phase
needed it, the same way `sec_filing`'s citation-validity path (ADR-0017)
needed no new code either.

Hard: `PEER_MAP` covers only five tickers — any ticker outside it produces
zero Competitive Agent claims (a documented, logged gap, not a crash; see
`competitive_agent_no_peers`). Extending coverage is manual curation work,
same as extending Phase 3's eval dataset. The inference/assumption gap in
point 3 is enforced by prompt instruction only, which is weaker than a
structural guarantee — worth revisiting if a real run shows Claude
circumventing it in practice.
