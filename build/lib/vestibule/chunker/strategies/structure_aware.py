"""StructureAwareChunkStrategy — flat, single-active-heading `section_path` (REQ-006).

Applies to any non-`TABLE` region of a document containing `HEADING` elements. Owns
only heading-boundary bookkeeping and flat `section_path` construction (LLD Assumption
A1) — a `HEADING` element's own text becomes the active breadcrumb, replaced (not
pushed/popped) on every subsequent `HEADING`; nesting is not fabricated from data the
Analyzer does not yet provide. Delegates any section's actual splitting to an injected
`RecursiveChunkStrategy` (LLD Assumption A4) — no token-boundary algorithm of its own.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Sequence

from vestibule.analyzer.model import Element, ElementType
from vestibule.chunker.model import STRATEGY_STRUCTURE_AWARE, ChunkDraft
from vestibule.chunker.strategies.base import ChunkStrategy
from vestibule.chunker.strategies.recursive import RecursiveChunkStrategy
from vestibule.chunker.token_counter import TokenCounter

if TYPE_CHECKING:
    from vestibule.chunker.chunker import ChunkerConfig


class StructureAwareChunkStrategy(ChunkStrategy):
    """Elements containing `HEADING` boundaries.

    Owns only heading-boundary bookkeeping and flat `section_path` construction (LLD
    Assumption A1); delegates any section's splitting to an injected
    `RecursiveChunkStrategy` (LLD Assumption A4) — no token-boundary algorithm of its
    own.
    """

    def __init__(self, recursive: RecursiveChunkStrategy) -> None:
        """Initializes the strategy.

        Args:
            recursive: The `RecursiveChunkStrategy` each section's content is delegated
                to for its actual splitting.
        """
        self._recursive = recursive

    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: "ChunkerConfig",
        token_counter: TokenCounter,
    ) -> list[ChunkDraft]:
        """See `ChunkStrategy.chunk_region`.

        Args:
            elements: One contiguous non-`TABLE` region, possibly containing `HEADING`
                elements.
            config: Passed straight through to the injected `RecursiveChunkStrategy`.
            token_counter: Passed straight through to the injected
                `RecursiveChunkStrategy`.

        Returns:
            One or more `ChunkDraft`s per section, each tagged
            `metadata["strategy"] == STRATEGY_STRUCTURE_AWARE` and
            `metadata["section_path"]` equal to that section's active heading text (or
            `None` for content preceding the region's first `HEADING`).
        """
        drafts: list[ChunkDraft] = []
        for section_path, section_elements in _split_into_sections(elements):
            section_drafts = self._recursive.chunk_region(
                section_elements, config=config, token_counter=token_counter
            )
            drafts.extend(_retag(draft, section_path) for draft in section_drafts)
        return drafts


def _split_into_sections(
    elements: Sequence[Element],
) -> list[tuple[str | None, list[Element]]]:
    """Groups `elements` by the most recently seen `HEADING`'s text (LLD Assumption A1).

    Args:
        elements: One contiguous non-`TABLE` region.

    Returns:
        `(section_path, content_elements)` pairs, in document order. Content preceding
        the region's first `HEADING` (if any) forms a leading section with
        `section_path=None`. A `HEADING` followed by no content before the next
        `HEADING`/end-of-region contributes no section (nothing to chunk).
    """
    sections: list[tuple[str | None, list[Element]]] = []
    current_path: str | None = None
    current_content: list[Element] = []
    for element in elements:
        if element.type is ElementType.HEADING:
            if current_content:
                sections.append((current_path, current_content))
            current_path = element.text
            current_content = []
            continue
        current_content.append(element)
    if current_content:
        sections.append((current_path, current_content))
    return sections


def _retag(draft: ChunkDraft, section_path: str | None) -> ChunkDraft:
    """Returns a copy of `draft` tagged with this strategy's name and `section_path`.

    Args:
        draft: The `ChunkDraft` produced by the injected `RecursiveChunkStrategy`.
        section_path: The active heading breadcrumb for `draft`'s section.

    Returns:
        A new `ChunkDraft` (frozen, so a copy) with `metadata["strategy"]` overwritten
        to `STRATEGY_STRUCTURE_AWARE` and `metadata["section_path"]` set to
        `section_path`.
    """
    metadata = dict(draft.metadata)
    metadata["strategy"] = STRATEGY_STRUCTURE_AWARE
    metadata["section_path"] = section_path
    return dataclasses.replace(draft, metadata=metadata)
