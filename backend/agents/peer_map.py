"""Hand-curated peer groups for the Competitive Agent (CLAUDE.md §7: peer
selection "from SIC/curated map"; §15: "peers from a curated map").

WHY curated, not SIC-code lookup: SIC codes are decades-old, broad
industry buckets that misclassify modern large-caps — e.g. Apple's own SIC
code (3663, "Radio & TV Broadcasting & Communications Equipment") reflects
a 1980s-era industry taxonomy, not what Apple actually competes in today.
Hand-curation is more accurate for a small, well-known set of large caps
than trusting SIC proximity, and CLAUDE.md's own "curated map" wording
anticipates exactly this.

STARTER SET — same "extend later" precedent as Phase 3's 15-question
retrieval eval starter set (evaluation/datasets/retrieval_eval.jsonl).
Peers are chosen by business-model overlap, not GICS sub-industry
precision, and are a genuine judgment call worth revisiting as more
tickers are covered.
"""

PEER_MAP: dict[str, tuple[str, ...]] = {
    "AAPL": ("MSFT", "GOOGL", "AMZN"),
    "MSFT": ("AAPL", "GOOGL", "AMZN"),
    "GOOGL": ("MSFT", "AAPL", "META"),
    "AMZN": ("MSFT", "GOOGL", "WMT"),
    "META": ("GOOGL", "SNAP", "PINS"),
}
