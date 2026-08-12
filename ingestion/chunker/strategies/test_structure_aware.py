"""Unit tests for StructureAwareChunkStrategy (REQ-006)."""

from __future__ import annotations

from typing import Sequence

from ingestion.analyzer.model import Element
from ingestion.chunker.chunker import ChunkerConfig
from ingestion.chunker.conftest import heading, paragraph
from ingestion.chunker.model import STRATEGY_STRUCTURE_AWARE, ChunkDraft
from ingestion.chunker.strategies.recursive import RecursiveChunkStrategy
from ingestion.chunker.strategies.structure_aware import StructureAwareChunkStrategy
from ingestion.chunker.token_counter import TiktokenCounter, TokenCounter

_LOREM_SENTENCE = (
    "The quick brown fox jumps over the lazy dog near the riverbank at dawn. "
)


def _long_text(sentence_count: int) -> str:
    return "\n\n".join(_LOREM_SENTENCE * 3 for _ in range(sentence_count))


class _SpyRecursiveStrategy(RecursiveChunkStrategy):
    """Records every `chunk_region` call while delegating to the real splitter."""

    def __init__(self) -> None:
        self.calls: list[Sequence[Element]] = []

    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: ChunkerConfig,
        token_counter: TokenCounter,
    ) -> list[ChunkDraft]:
        self.calls.append(elements)
        return super().chunk_region(
            elements, config=config, token_counter=token_counter
        )


# --- section breadcrumb (Assumption A1) -----------------------------------------------------


def test_structure_aware_chunker_chunks_carry_section_breadcrumb() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = StructureAwareChunkStrategy(RecursiveChunkStrategy())
    elements = [
        heading("1. Scope"),
        paragraph("Content under Scope."),
        paragraph("More content under Scope."),
    ]

    drafts = strategy.chunk_region(
        elements, config=ChunkerConfig(), token_counter=counter
    )

    assert len(drafts) >= 1
    assert all(draft.metadata["section_path"] == "1. Scope" for draft in drafts)
    assert all(
        draft.metadata["strategy"] == STRATEGY_STRUCTURE_AWARE for draft in drafts
    )
    joined_text = " ".join(draft.text for draft in drafts)
    assert "Content under Scope." in joined_text
    assert "More content under Scope." in joined_text


def test_structure_aware_chunker_content_before_first_heading_has_null_section_path() -> (
    None
):
    counter = TiktokenCounter("cl100k_base")
    strategy = StructureAwareChunkStrategy(RecursiveChunkStrategy())
    elements = [paragraph("Preamble content."), heading("1. Scope"), paragraph("Body.")]

    drafts = strategy.chunk_region(
        elements, config=ChunkerConfig(), token_counter=counter
    )

    assert drafts[0].metadata["section_path"] is None
    assert drafts[1].metadata["section_path"] == "1. Scope"


def test_structure_aware_chunker_heading_replaced_not_nested_on_new_heading() -> None:
    """Flat, single-active-heading design (Assumption A1): a second HEADING replaces
    the breadcrumb rather than nesting under the first."""
    counter = TiktokenCounter("cl100k_base")
    strategy = StructureAwareChunkStrategy(RecursiveChunkStrategy())
    elements = [
        heading("1. Scope"),
        paragraph("Scope content."),
        heading("2. Definitions"),
        paragraph("Definitions content."),
    ]

    drafts = strategy.chunk_region(
        elements, config=ChunkerConfig(), token_counter=counter
    )

    section_paths = [draft.metadata["section_path"] for draft in drafts]
    assert section_paths == ["1. Scope", "2. Definitions"]


def test_structure_aware_chunker_no_level_metadata_degrades_to_flat_breadcrumb() -> (
    None
):
    """Regression test for Assumption A1: nested-looking heading text does not produce
    a fabricated multi-level breadcrumb when metadata.get("level") is absent."""
    counter = TiktokenCounter("cl100k_base")
    strategy = StructureAwareChunkStrategy(RecursiveChunkStrategy())
    elements = [
        heading("1. Scope"),
        paragraph("Scope content."),
        heading("1.2 Definitions"),  # nested-looking text, no metadata["level"] present
        paragraph("Definitions content."),
    ]

    drafts = strategy.chunk_region(
        elements, config=ChunkerConfig(), token_counter=counter
    )

    section_paths = {draft.metadata["section_path"] for draft in drafts}
    assert section_paths == {"1. Scope", "1.2 Definitions"}
    assert " > " not in " ".join(str(p) for p in section_paths)


def test_structure_aware_chunker_heading_with_no_following_content_yields_no_section() -> (
    None
):
    counter = TiktokenCounter("cl100k_base")
    strategy = StructureAwareChunkStrategy(RecursiveChunkStrategy())
    elements = [heading("1. Scope"), heading("2. Definitions"), paragraph("Body.")]

    drafts = strategy.chunk_region(
        elements, config=ChunkerConfig(), token_counter=counter
    )

    assert len(drafts) == 1
    assert drafts[0].metadata["section_path"] == "2. Definitions"


def test_structure_aware_chunker_empty_region_returns_empty_list() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = StructureAwareChunkStrategy(RecursiveChunkStrategy())

    drafts = strategy.chunk_region([], config=ChunkerConfig(), token_counter=counter)

    assert drafts == []


# --- heading boundaries (story) ---------------------------------------------------------


def test_structure_aware_chunker_respects_heading_boundaries() -> None:
    counter = TiktokenCounter("cl100k_base")
    config = ChunkerConfig(target_chunk_tokens=50, overlap_tokens=5)
    strategy = StructureAwareChunkStrategy(RecursiveChunkStrategy())
    scope_sentence = "The quick brown fox jumps over the lazy dog near the riverbank. "
    definitions_sentence = "A glossary term is defined precisely in this section here. "
    elements = [
        heading("1. Scope"),
        paragraph(scope_sentence * 30),
        heading("2. Definitions"),
        paragraph(definitions_sentence * 30),
    ]

    drafts = strategy.chunk_region(elements, config=config, token_counter=counter)

    scope_chunks = [d for d in drafts if d.metadata["section_path"] == "1. Scope"]
    definitions_chunks = [
        d for d in drafts if d.metadata["section_path"] == "2. Definitions"
    ]
    assert len(scope_chunks) > 1
    assert len(definitions_chunks) > 1
    for chunk in scope_chunks:
        assert "riverbank" in chunk.text
        assert "glossary" not in chunk.text
    for chunk in definitions_chunks:
        assert "glossary" in chunk.text
        assert "riverbank" not in chunk.text


# --- oversized section falls back to recursive split (Assumption A4) -----------------------


def test_structure_aware_chunker_oversized_section_falls_back_to_recursive_split() -> (
    None
):
    counter = TiktokenCounter("cl100k_base")
    config = ChunkerConfig(target_chunk_tokens=50, overlap_tokens=5)
    spy = _SpyRecursiveStrategy()
    strategy = StructureAwareChunkStrategy(spy)
    elements = [heading("1. Scope"), paragraph(_long_text(15))]

    drafts = strategy.chunk_region(elements, config=config, token_counter=counter)

    assert len(spy.calls) == 1  # delegated to the injected RecursiveChunkStrategy
    assert len(drafts) > 1
    assert all(draft.metadata["section_path"] == "1. Scope" for draft in drafts)
