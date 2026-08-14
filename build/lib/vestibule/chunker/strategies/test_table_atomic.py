"""Unit tests for TableAtomicChunkStrategy (REQ-006)."""

from __future__ import annotations

import logging

import pytest

from vestibule.chunker.chunker import ChunkerConfig
from vestibule.chunker.conftest import table
from vestibule.chunker.model import OVERSIZED_TABLE_LOG_EVENT, STRATEGY_TABLE_ATOMIC
from vestibule.chunker.strategies.table_atomic import TableAtomicChunkStrategy
from vestibule.chunker.token_counter import TiktokenCounter


# --- AC1/AC2 (story) ---------------------------------------------------------------------


def test_table_atomic_20_row_table_produces_exactly_one_chunk() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    rows = [[f"r{r}c{c}" for c in range(3)] for r in range(20)]
    element = table(rows)

    drafts = strategy.chunk_region(
        [element], config=ChunkerConfig(), token_counter=counter
    )

    assert len(drafts) == 1
    for r in range(20):
        assert f"r{r}c0" in drafts[0].text
    assert drafts[0].metadata["strategy"] == STRATEGY_TABLE_ATOMIC


def test_table_atomic_30_row_table_produces_exactly_one_chunk_regardless_of_size() -> (
    None
):
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    rows = [[f"row {r} col {c}" for c in range(5)] for r in range(30)]
    element = table(rows)

    drafts = strategy.chunk_region(
        [element], config=ChunkerConfig(max_chunk_tokens=10), token_counter=counter
    )

    assert len(drafts) == 1


# --- F4: oversized-table advisory (design-review BLOCKER fix) ------------------------------


def test_table_atomic_table_larger_than_max_chunk_tokens_emits_one_chunk_plus_warning_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    rows = [
        [f"row {r} col {c} with some extra content" for c in range(4)]
        for r in range(20)
    ]
    element = table(rows)
    config = ChunkerConfig(max_chunk_tokens=10)

    with caplog.at_level(
        logging.WARNING, logger="vestibule.chunker.strategies.table_atomic"
    ):
        drafts = strategy.chunk_region(
            [element], config=config, token_counter=counter, doc_id="d" * 64
        )

    assert len(drafts) == 1
    assert drafts[0].metadata["oversized"] is True
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == OVERSIZED_TABLE_LOG_EVENT
    assert warnings[0].doc_id == "d" * 64  # type: ignore[attr-defined]
    assert warnings[0].table_tokens == drafts[0].token_count  # type: ignore[attr-defined]
    assert warnings[0].max_tokens == 10  # type: ignore[attr-defined]


def test_table_atomic_table_within_max_chunk_tokens_no_warning_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    element = table([["a", "b"], ["c", "d"]])

    with caplog.at_level(
        logging.WARNING, logger="vestibule.chunker.strategies.table_atomic"
    ):
        drafts = strategy.chunk_region(
            [element], config=ChunkerConfig(), token_counter=counter
        )

    assert drafts[0].metadata["oversized"] is False
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


# --- Assumption A2 caption adjacency -----------------------------------------------------


def test_table_atomic_prepends_adjacent_caption_when_present() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    element = table([["a", "b"], ["c", "d"]])

    drafts = strategy.chunk_region(
        [element],
        config=ChunkerConfig(),
        token_counter=counter,
        caption_text="Table 1: Sample data",
    )

    assert drafts[0].text.startswith("Table 1: Sample data")


def test_table_atomic_no_caption_when_not_adjacent() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    element = table([["a", "b"], ["c", "d"]])

    drafts = strategy.chunk_region(
        [element], config=ChunkerConfig(), token_counter=counter, caption_text=None
    )

    assert not drafts[0].text.startswith("Table")


# --- cells-metadata-absent fallback --------------------------------------------------------


def test_table_atomic_falls_back_to_element_text_when_cells_metadata_absent() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    element = table([["a", "b"], ["c", "d"]], text="a\nb\nc\nd", with_cells=False)

    drafts = strategy.chunk_region(
        [element], config=ChunkerConfig(), token_counter=counter
    )

    assert drafts[0].text == "a\nb\nc\nd"


def test_table_atomic_markdown_serialization_includes_header_and_separator() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    element = table([["Name", "Age"], ["Alice", "30"]])

    drafts = strategy.chunk_region(
        [element], config=ChunkerConfig(), token_counter=counter
    )

    lines = drafts[0].text.splitlines()
    assert lines[0] == "| Name | Age |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| Alice | 30 |"


def test_table_atomic_never_splits_returns_exactly_one_draft() -> None:
    counter = TiktokenCounter("cl100k_base")
    strategy = TableAtomicChunkStrategy()
    rows = [[f"cell {r}-{c}" for c in range(10)] for r in range(50)]
    element = table(rows)

    drafts = strategy.chunk_region(
        [element], config=ChunkerConfig(max_chunk_tokens=1), token_counter=counter
    )

    assert len(drafts) == 1
