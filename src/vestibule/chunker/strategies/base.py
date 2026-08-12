"""ChunkStrategy contract and small shared helpers (REQ-006).

`ChunkStrategy` is the thin strategy contract every chunking strategy implements
(house rules: "adapters thin") — a strategy composes an external splitter/tokenizer via
`TokenCounter`/`RecursiveChunkStrategy`, never hand-rolls a splitting algorithm of its
own (LLD Assumption A4). `join_element_text`/`unique_element_types`/`page_metadata` are
small bookkeeping helpers shared by every concrete strategy so none of them duplicates
this logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Sequence

from vestibule.analyzer.model import Element, ElementType
from vestibule.chunker.model import ChunkDraft
from vestibule.chunker.token_counter import TokenCounter

if TYPE_CHECKING:
    from vestibule.chunker.chunker import ChunkerConfig


class ChunkStrategy(ABC):
    """Thin strategy contract (house rules: "adapters thin").

    A strategy composes an external splitter/tokenizer via `TokenCounter`/
    `RecursiveChunkStrategy`, never hand-rolls a splitting algorithm of its own (LLD
    Assumption A4).
    """

    @abstractmethod
    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: "ChunkerConfig",
        token_counter: TokenCounter,
    ) -> list[ChunkDraft]:
        """Chunks one contiguous, same-kind region of elements.

        Args:
            elements: One contiguous, same-kind run from the orchestrator's
                partitioning (LLD §3 step 3) — never the whole document.
            config: Operational tunables.
            token_counter: Used for every token-count computation, so accounting stays
                consistent across strategies.

        Returns:
            The `ChunkDraft`s produced from `elements`, in document order.

        Raises:
            ChunkerError: `TOKENIZER_LOAD_FAILED`, if raised by `token_counter` itself.
                Never raises an element-count-related error directly — `Chunker` owns
                `CHUNKER_EMPTY_ELEMENTS`. Any other exception may propagate unchanged
                for `Chunker` to wrap as `CHUNKER_INTERNAL`.
        """
        raise NotImplementedError  # pragma: no cover


def join_element_text(elements: Sequence[Element]) -> str:
    """Joins `elements`' text in document order, separated by a blank line.

    Args:
        elements: The elements to join.

    Returns:
        The joined text; `""` if `elements` is empty.
    """
    return "\n\n".join(element.text for element in elements)


def unique_element_types(elements: Sequence[Element]) -> list[ElementType]:
    """Every distinct `Element.type` among `elements`, in first-seen order.

    Args:
        elements: The elements to inspect.

    Returns:
        A list of distinct `ElementType`s, in first-seen order.
    """
    seen: list[ElementType] = []
    for element in elements:
        if element.type not in seen:
            seen.append(element.type)
    return seen


def page_metadata(elements: Sequence[Element]) -> dict[str, Any]:
    """Derives a `page`/`pages` metadata entry from `elements`' own metadata, if known.

    Args:
        elements: The elements to inspect for a `metadata["page"]` integer.

    Returns:
        `{"page": p}` if every element carrying a page number agrees on exactly one
        page; `{"pages": [...]}` (sorted, unique) if more than one distinct page is
        found; `{}` if no element carries a `page` integer at all.
    """
    pages: set[int] = set()
    for element in elements:
        page = element.metadata.get("page")
        if isinstance(page, int):
            pages.add(page)
    if not pages:
        return {}
    sorted_pages = sorted(pages)
    if len(sorted_pages) == 1:
        return {"page": sorted_pages[0]}
    return {"pages": sorted_pages}
