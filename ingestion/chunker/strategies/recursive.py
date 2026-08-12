"""RecursiveChunkStrategy — thin wrap of `RecursiveCharacterTextSplitter` (REQ-006).

Applies to `PARAGRAPH`/`LIST`/`CODE` elements, and any non-`TABLE` region of a
heading-free document. Thin wrap of
`langchain_text_splitters.RecursiveCharacterTextSplitter` (LLD Assumption A4; the
story's own References section names this exact class as "reference implementation"),
configured with `length_function=token_counter.count` so token accounting stays
consistent with the rest of this module. Applying the `min_chunk_tokens` trailing-merge
pass is *not* this class's job — `Chunker`'s orchestrator does it once per region (LLD
Assumption A8), keeping this class a thin, mergeless splitter wrapper.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

from langchain_text_splitters import RecursiveCharacterTextSplitter

from ingestion.analyzer.model import Element
from ingestion.chunker.model import STRATEGY_RECURSIVE, ChunkDraft
from ingestion.chunker.strategies.base import (
    ChunkStrategy,
    join_element_text,
    page_metadata,
    unique_element_types,
)
from ingestion.chunker.token_counter import TokenCounter

if TYPE_CHECKING:
    from ingestion.chunker.chunker import ChunkerConfig


class RecursiveChunkStrategy(ChunkStrategy):
    """`PARAGRAPH`/`LIST`/`CODE` elements. Thin wrap of `RecursiveCharacterTextSplitter`."""

    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: "ChunkerConfig",
        token_counter: TokenCounter,
    ) -> list[ChunkDraft]:
        """See `ChunkStrategy.chunk_region`.

        Args:
            elements: One contiguous non-`TABLE` region. May be empty (e.g. a
                `StructureAwareChunkStrategy` section with no content elements), in
                which case `[]` is returned.
            config: `target_chunk_tokens`/`overlap_tokens` drive the underlying
                splitter's `chunk_size`/`chunk_overlap`.
            token_counter: Used both as the splitter's `length_function` and to compute
                each resulting draft's `token_count`.

        Returns:
            One `ChunkDraft` per split piece, each tagged
            `metadata["strategy"] == STRATEGY_RECURSIVE` and
            `metadata["section_path"] is None`.
        """
        if not elements:
            return []
        text = join_element_text(elements)
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=config.target_chunk_tokens,
            chunk_overlap=config.overlap_tokens,
            length_function=token_counter.count,
        )
        element_types = unique_element_types(elements)
        base_metadata: dict[str, Any] = {
            "strategy": STRATEGY_RECURSIVE,
            "section_path": None,
            **page_metadata(elements),
        }
        return [
            ChunkDraft(
                element_types=list(element_types),
                text=piece,
                token_count=token_counter.count(piece),
                metadata=dict(base_metadata),
            )
            for piece in splitter.split_text(text)
        ]
