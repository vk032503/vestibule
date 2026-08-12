"""TableAtomicChunkStrategy — one chunk per `TABLE` element (REQ-006).

Serializes `metadata["cells"]` (REQ-005's `docint_parser` shape: `row_index`/
`column_index`/`content`) to a markdown table; falls back to `element.text` (a plain
reading-order join) if `"cells"` is absent (e.g. a future non-Document-Intelligence
table source). Never splits — always returns exactly one `ChunkDraft`, regardless of
`token_count` (story AC2). Table serialization is this module's own job (REQ-005's
`docint_parser` explicitly leaves "how a chunker should flatten this into text" to the
chunker, LLD Assumption A4) — never a splitting algorithm.

When the resulting `token_count` exceeds `config.max_chunk_tokens`, logs
`OVERSIZED_TABLE_LOG_EVENT` and continues — no exception, no error code (LLD
Assumption A3, design-review BLOCKER fix).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Sequence

from ingestion.analyzer.model import Element
from ingestion.chunker.model import (
    OVERSIZED_TABLE_LOG_EVENT,
    STRATEGY_TABLE_ATOMIC,
    ChunkDraft,
)
from ingestion.chunker.strategies.base import ChunkStrategy, page_metadata
from ingestion.chunker.token_counter import TokenCounter

if TYPE_CHECKING:
    from ingestion.chunker.chunker import ChunkerConfig

logger = logging.getLogger(__name__)

_DOC_ID_UNKNOWN = ""


class TableAtomicChunkStrategy(ChunkStrategy):
    """Exactly one `TABLE` element per call (orchestrator invariant, LLD §3 step 3)."""

    def chunk_region(
        self,
        elements: Sequence[Element],
        *,
        config: "ChunkerConfig",
        token_counter: TokenCounter,
        caption_text: str | None = None,
        doc_id: str = _DOC_ID_UNKNOWN,
    ) -> list[ChunkDraft]:
        """See `ChunkStrategy.chunk_region`.

        Args:
            elements: Always `[table_element]` (orchestrator invariant).
            config: `max_chunk_tokens` bounds the oversized-table advisory check.
            token_counter: Used to compute the serialized table's `token_count`.
            caption_text: Supplied by the orchestrator per the LLD's Assumption A2
                adjacency heuristic, not derived here.
            doc_id: Included in the oversized-table log's `extra`, if known. Not part
                of the base `ChunkStrategy.chunk_region` contract — an additional
                optional keyword this class accepts, exactly like `caption_text`, so a
                useful `doc_id` can be logged when the orchestrator (which has it)
                dispatches here.

        Returns:
            Exactly one `ChunkDraft`, always — never splits a table (story AC2). Its
            `metadata["oversized"]` is `True` iff `token_count > config.max_chunk_tokens`.
        """
        table = elements[0]
        text = _serialize_table(table, caption_text)
        token_count = token_counter.count(text)
        oversized = token_count > config.max_chunk_tokens
        if oversized:
            logger.warning(
                OVERSIZED_TABLE_LOG_EVENT,
                extra={
                    "doc_id": doc_id,
                    "table_tokens": token_count,
                    "max_tokens": config.max_chunk_tokens,
                },
            )
        metadata: dict[str, Any] = {
            "strategy": STRATEGY_TABLE_ATOMIC,
            "section_path": None,
            "oversized": oversized,
            **page_metadata([table]),
        }
        return [
            ChunkDraft(
                element_types=[table.type],
                text=text,
                token_count=token_count,
                metadata=metadata,
            )
        ]


def _serialize_table(table: Element, caption_text: str | None) -> str:
    """Markdown-serializes `table`'s cells; falls back to `table.text` if absent.

    Args:
        table: The `TABLE` element to serialize.
        caption_text: Prepended before the table body, if not `None` (LLD Assumption A2).

    Returns:
        The serialized table text, with the caption (if any) prepended.
    """
    cells = table.metadata.get("cells")
    body = _markdown_table(cells) if cells else table.text
    return f"{caption_text}\n\n{body}" if caption_text else body


def _markdown_table(cells: list[dict[str, Any]]) -> str:
    """Renders row/column `cells` (REQ-005 `docint_parser` shape) as a markdown table.

    Args:
        cells: `{"row_index": int, "column_index": int, "content": str}` dicts.

    Returns:
        A markdown table: a header row (the table's first row), a separator row, then
        every remaining row.
    """
    row_count = max(cell["row_index"] for cell in cells) + 1
    column_count = max(cell["column_index"] for cell in cells) + 1
    grid = [["" for _ in range(column_count)] for _ in range(row_count)]
    for cell in cells:
        grid[cell["row_index"]][cell["column_index"]] = str(cell.get("content", ""))
    header = "| " + " | ".join(grid[0]) + " |"
    separator = "| " + " | ".join("---" for _ in range(column_count)) + " |"
    body_rows = ["| " + " | ".join(row) + " |" for row in grid[1:]]
    return "\n".join([header, separator, *body_rows])
