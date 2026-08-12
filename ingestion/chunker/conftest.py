"""Shared fixtures and helpers for the Chunker test suite (REQ-006).

`make_envelope` is re-exported from `ingestion.analyzer.conftest` (REQ-005) rather than
re-implemented, so every test suite in this repo builds envelopes the same way. The
`Element` factories below are plain helper functions (not pytest fixtures), shared
across this package's test modules by direct import.
"""

from __future__ import annotations

from typing import Any

from ingestion.analyzer.conftest import make_envelope
from ingestion.analyzer.model import Element, ElementType

__all__ = [
    "make_envelope",
    "heading",
    "paragraph",
    "code_block",
    "list_item",
    "table",
    "image_caption",
]


def paragraph(text: str, *, page: int | None = None) -> Element:
    """Builds a `PARAGRAPH` `Element`, optionally carrying a `page` metadata entry."""
    return Element(type=ElementType.PARAGRAPH, text=text, metadata=_page_meta(page))


def heading(text: str, *, page: int | None = None) -> Element:
    """Builds a `HEADING` `Element`, optionally carrying a `page` metadata entry."""
    return Element(type=ElementType.HEADING, text=text, metadata=_page_meta(page))


def code_block(text: str, *, page: int | None = None) -> Element:
    """Builds a `CODE` `Element`, optionally carrying a `page` metadata entry."""
    return Element(type=ElementType.CODE, text=text, metadata=_page_meta(page))


def list_item(text: str, *, page: int | None = None) -> Element:
    """Builds a `LIST` `Element`, optionally carrying a `page` metadata entry."""
    return Element(type=ElementType.LIST, text=text, metadata=_page_meta(page))


def image_caption(text: str, *, page: int | None = None) -> Element:
    """Builds an `IMAGE_CAPTION` `Element`, optionally carrying a `page` metadata entry."""
    return Element(type=ElementType.IMAGE_CAPTION, text=text, metadata=_page_meta(page))


def table(
    rows: list[list[str]],
    *,
    text: str | None = None,
    page: int | None = None,
    with_cells: bool = True,
) -> Element:
    """Builds a `TABLE` `Element` from a row-major grid of cell strings.

    Args:
        rows: Row-major grid; `rows[r][c]` is the cell content at row `r`, column `c`.
        text: Overrides the fallback `element.text` (defaults to a reading-order join
            of every cell, matching REQ-005's `docint_parser` convention).
        page: Optional page number metadata.
        with_cells: If `False`, omits `metadata["cells"]` entirely (REQ-005's
            docint-absent case, e.g. a future non-Document-Intelligence table source).

    Returns:
        A `TABLE` `Element` with `metadata["cells"]` in REQ-005's `docint_parser` shape.
    """
    flat_text = (
        text if text is not None else "\n".join(cell for row in rows for cell in row)
    )
    metadata: dict[str, Any] = _page_meta(page)
    metadata["row_count"] = len(rows)
    metadata["column_count"] = len(rows[0]) if rows else 0
    if with_cells:
        metadata["cells"] = [
            {"row_index": r, "column_index": c, "content": content}
            for r, row in enumerate(rows)
            for c, content in enumerate(row)
        ]
    return Element(type=ElementType.TABLE, text=flat_text, metadata=metadata)


def _page_meta(page: int | None) -> dict[str, Any]:
    """`{"page": page}` if `page` is not `None`, else `{}`."""
    return {} if page is None else {"page": page}
