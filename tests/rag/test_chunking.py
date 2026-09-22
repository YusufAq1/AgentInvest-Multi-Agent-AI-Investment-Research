"""Tests for backend.rag.chunking.

WHY get_tokenizer is monkeypatched everywhere: the real BGE-M3 tokenizer
would force a ~2GB model download on first use (see
backend/rag/embeddings.py's module docstring) — this suite proves
chunking's reconstruction and sub-chunking logic against a fake tokenizer
whose behavior is fully specified in-test, never a real model.

WHY there's no test exercising offset "trust" against a separately fetched
document text: an earlier version of this module tried exactly that
design, and real end-to-end testing against a live AAPL 10-K falsified it
— edgartools' Section.start_offset/end_offset aren't usable global
offsets, and Section.text() isn't relocatable inside Document.text() via
substring search either (see chunking.py's module docstring and
ADR-0015's revision history). This suite tests the design that replaced
it: build_full_text() reconstructs the text FROM the sections, so offsets
are correct by construction.
"""

from datetime import date
from unittest.mock import MagicMock, patch

from backend.data.models import SectionMeta
from backend.rag.chunking import (
    SectionChunk,
    _sub_chunk_offsets,
    build_full_text,
    chunk_filing_sections,
)

ACCESSION = "0000320193-24-000123"
FILING_DATE = date(2024, 2, 1)


def _section(
    *,
    name: str = "risk_factors",
    item: str | None = "1A",
    confidence: float = 0.95,
    detection_method: str = "toc",
    text: str,
) -> SectionMeta:
    return SectionMeta(
        name=name,
        title="Item 1A - Risk Factors",
        item=item,
        part=None,
        confidence=confidence,
        detection_method=detection_method,
        text=text,
    )


class _FakeWordTokenizer:
    """A trivial whitespace tokenizer with exact char offsets — good
    enough to exercise `_sub_chunk_offsets`' windowing/overlap math
    without needing the real BGE-M3 subword tokenizer.
    """

    def __call__(
        self, text: str, *, return_offsets_mapping: bool, add_special_tokens: bool
    ) -> dict[str, list[tuple[int, int]]]:
        assert return_offsets_mapping and not add_special_tokens
        offsets: list[tuple[int, int]] = []
        pos = 0
        for word in text.split(" "):
            start = text.index(word, pos)
            end = start + len(word)
            offsets.append((start, end))
            pos = end
        return {"offset_mapping": offsets}


def test_build_full_text_joins_sections_with_blank_line() -> None:
    sections = [
        _section(name="item_1", item="1", text="Business overview."),
        _section(name="item_1a", item="1A", text="Risk factors."),
    ]

    full_text = build_full_text(sections)

    assert full_text == "Business overview.\n\nRisk factors."


def test_build_full_text_empty_sections_is_empty_string() -> None:
    assert build_full_text([]) == ""


@patch("backend.rag.chunking.get_tokenizer")
def test_chunk_offsets_are_correct_against_build_full_text(
    mock_get_tokenizer: MagicMock,
) -> None:
    mock_get_tokenizer.return_value = _FakeWordTokenizer()
    sections = [
        _section(name="item_1", item="1", text="Business overview body."),
        _section(name="item_1a", item="1A", text="Risk factors body."),
    ]
    full_text = build_full_text(sections)

    chunks = chunk_filing_sections(
        sections=sections,
        accession=ACCESSION,
        filing_date=FILING_DATE,
        fiscal_period="FY2023",
        max_tokens=512,
        overlap_tokens=64,
        confidence_floor=0.6,
    )

    assert len(chunks) == 2
    for chunk in chunks:
        assert full_text[chunk.char_start : chunk.char_end] == chunk.text


@patch("backend.rag.chunking.get_tokenizer")
def test_low_confidence_section_is_skipped_and_logged(mock_get_tokenizer: MagicMock) -> None:
    mock_get_tokenizer.return_value = _FakeWordTokenizer()
    sections = [
        _section(name="item_1", item="1", confidence=0.95, text="Business overview body."),
        _section(
            name="item_1c",
            item="1C",
            confidence=0.5,  # below the default 0.6 floor
            detection_method="toc",
            text="Cybersecurity disclosures body.",
        ),
    ]

    chunks = chunk_filing_sections(
        sections=sections,
        accession=ACCESSION,
        filing_date=FILING_DATE,
        fiscal_period="FY2023",
        max_tokens=512,
        overlap_tokens=64,
        confidence_floor=0.6,
    )

    assert len(chunks) == 1
    assert chunks[0].item == "1"


@patch("backend.rag.chunking.get_tokenizer")
def test_empty_section_text_produces_no_chunks(mock_get_tokenizer: MagicMock) -> None:
    mock_get_tokenizer.return_value = _FakeWordTokenizer()
    sections = [_section(name="item_1", item="1", text="")]

    chunks = chunk_filing_sections(
        sections=sections,
        accession=ACCESSION,
        filing_date=FILING_DATE,
        fiscal_period=None,
        max_tokens=512,
        overlap_tokens=64,
        confidence_floor=0.6,
    )

    assert chunks == []


@patch("backend.rag.chunking.get_tokenizer")
def test_chunk_filing_sections_carries_metadata_through(mock_get_tokenizer: MagicMock) -> None:
    mock_get_tokenizer.return_value = _FakeWordTokenizer()
    sections = [_section(name="item_1a", item="1A", text="Risk factors body text.")]

    chunks = chunk_filing_sections(
        sections=sections,
        accession=ACCESSION,
        filing_date=FILING_DATE,
        fiscal_period="FY2023",
        max_tokens=512,
        overlap_tokens=64,
        confidence_floor=0.6,
    )

    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.item == "1A"
    assert chunk.section_name == "item_1a"
    assert chunk.accession == ACCESSION
    assert chunk.filing_date == FILING_DATE
    assert chunk.fiscal_period == "FY2023"
    assert chunk.chunk_index == 0


@patch("backend.rag.chunking.get_tokenizer")
def test_sub_chunk_offsets_windows_with_overlap(mock_get_tokenizer: MagicMock) -> None:
    mock_get_tokenizer.return_value = _FakeWordTokenizer()
    # 10 "words" -> 10 tokens under the fake tokenizer.
    text = " ".join(f"word{i}" for i in range(10))

    spans = _sub_chunk_offsets(text, max_tokens=4, overlap_tokens=1)

    # step = max_tokens - overlap_tokens = 3; windows start at token 0, 3, 6
    # (the third window covers tokens 6-9, reaching the end, so no 4th
    # window is needed).
    assert len(spans) == 3
    for start, end in spans:
        assert 0 <= start < end <= len(text)
    assert spans[-1][1] == len(text)
    assert spans[0][0] == 0


@patch("backend.rag.chunking.get_tokenizer")
def test_sub_chunk_offsets_single_window_when_short_enough(mock_get_tokenizer: MagicMock) -> None:
    mock_get_tokenizer.return_value = _FakeWordTokenizer()
    text = "short text body"

    spans = _sub_chunk_offsets(text, max_tokens=512, overlap_tokens=64)

    assert spans == [(0, len(text))]


def test_sub_chunk_offsets_empty_text_returns_no_spans() -> None:
    assert _sub_chunk_offsets("", max_tokens=512, overlap_tokens=64) == []


def test_section_chunk_is_a_pydantic_model_with_expected_fields() -> None:
    chunk = SectionChunk(
        text="body",
        chunk_index=0,
        item="1A",
        section_name="risk_factors",
        char_start=0,
        char_end=4,
        accession=ACCESSION,
        filing_date=FILING_DATE,
        fiscal_period=None,
    )
    assert chunk.text == "body"
    assert chunk.fiscal_period is None
