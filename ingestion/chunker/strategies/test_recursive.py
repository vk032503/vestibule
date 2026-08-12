"""Unit tests for RecursiveChunkStrategy (REQ-006)."""

from __future__ import annotations

from ingestion.analyzer.model import ElementType
from ingestion.chunker.chunker import ChunkerConfig
from ingestion.chunker.conftest import code_block, list_item, paragraph
from ingestion.chunker.model import STRATEGY_RECURSIVE
from ingestion.chunker.strategies.recursive import RecursiveChunkStrategy
from ingestion.chunker.token_counter import TiktokenCounter

_LOREM_SENTENCE = (
    "The quick brown fox jumps over the lazy dog near the riverbank at dawn. "
)


def _long_text(sentence_count: int) -> str:
    return "\n\n".join(_LOREM_SENTENCE * 3 for _ in range(sentence_count))


# --- size/overlap (AC3, story) -------------------------------------------------------------


def test_recursive_chunker_prose_only_document_respects_target_size_and_overlap() -> (
    None
):
    counter = TiktokenCounter("cl100k_base")
    config = ChunkerConfig(target_chunk_tokens=50, overlap_tokens=10)
    strategy = RecursiveChunkStrategy()

    drafts = strategy.chunk_region(
        [paragraph(_long_text(20))], config=config, token_counter=counter
    )

    assert len(drafts) > 1
    for draft in drafts[:-1]:
        assert draft.token_count <= config.target_chunk_tokens
    for earlier, later in zip(drafts, drafts[1:]):
        earlier_words = set(earlier.text.split())
        later_words = set(later.text.split())
        assert earlier_words & later_words  # some overlap between consecutive pieces


def test_recursive_chunker_single_small_paragraph_yields_one_chunk() -> None:
    counter = TiktokenCounter("cl100k_base")
    config = ChunkerConfig(target_chunk_tokens=512, overlap_tokens=64)
    strategy = RecursiveChunkStrategy()

    drafts = strategy.chunk_region(
        [paragraph("A short paragraph.")], config=config, token_counter=counter
    )

    assert len(drafts) == 1
    assert drafts[0].text == "A short paragraph."


def test_recursive_chunker_empty_region_returns_empty_list() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = RecursiveChunkStrategy()

    drafts = strategy.chunk_region([], config=ChunkerConfig(), token_counter=counter)

    assert drafts == []


# --- metadata tagging ------------------------------------------------------------------


def test_recursive_chunker_tags_strategy_and_null_section_path() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = RecursiveChunkStrategy()

    drafts = strategy.chunk_region(
        [paragraph("Some short text.")], config=ChunkerConfig(), token_counter=counter
    )

    assert drafts[0].metadata["strategy"] == STRATEGY_RECURSIVE
    assert drafts[0].metadata["section_path"] is None


def test_recursive_chunker_records_element_types_and_page() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = RecursiveChunkStrategy()

    drafts = strategy.chunk_region(
        [paragraph("Some text.", page=2), list_item("An item.", page=2)],
        config=ChunkerConfig(),
        token_counter=counter,
    )

    assert set(drafts[0].element_types) == {ElementType.PARAGRAPH, ElementType.LIST}
    assert drafts[0].metadata["page"] == 2


def test_recursive_chunker_records_multiple_pages_when_elements_span_pages() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = RecursiveChunkStrategy()

    drafts = strategy.chunk_region(
        [paragraph("Some text.", page=1), paragraph("More text.", page=2)],
        config=ChunkerConfig(),
        token_counter=counter,
    )

    assert drafts[0].metadata["pages"] == [1, 2]
    assert "page" not in drafts[0].metadata


def test_recursive_chunker_handles_code_elements() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = RecursiveChunkStrategy()

    drafts = strategy.chunk_region(
        [code_block("def f():\n    return 1")],
        config=ChunkerConfig(),
        token_counter=counter,
    )

    assert len(drafts) == 1
    assert "def f()" in drafts[0].text
