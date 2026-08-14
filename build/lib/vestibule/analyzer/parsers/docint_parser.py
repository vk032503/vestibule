"""DocumentIntelligenceParser — thin ParserAdapter wrapping Azure Document Intelligence
for SCANNED_PDF (REQ-005).

Extracts text, tables (with row/column structure), and section headings via the
`prebuilt-layout` model. Per-call timeout is Analyzer's responsibility (LLD Assumption
A5) — this adapter makes no timeout decision of its own.
"""

from __future__ import annotations

import io

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeResult,
    DocumentParagraph,
    DocumentTable,
    ParagraphRole,
)
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError

from vestibule.analyzer.model import (
    DOCINT_RATE_LIMITED,
    DOCINT_UPSTREAM_ERROR,
    AnalyzerError,
    BytesReader,
    Element,
    ElementType,
)
from vestibule.analyzer.registry import ParserAdapter
from vestibule.envelope.model import ArrivalEnvelope

_DEFAULT_MODEL_ID = "prebuilt-layout"
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_FLOOR = 500
_HEADING_ROLES = frozenset({ParagraphRole.TITLE, ParagraphRole.SECTION_HEADING})


class DocumentIntelligenceParser(ParserAdapter):
    """Routes for `DetectedType.SCANNED_PDF`. Thin wrap of Azure Document
    Intelligence's `prebuilt-layout` model.

    `endpoint`/`api_key` are injected by the composition root from env only (house
    rules: no secrets in code); this class never reads env itself.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        api_version: str,
        model_id: str = _DEFAULT_MODEL_ID,
    ) -> None:
        """Initializes the parser with an Azure Document Intelligence client.

        Args:
            endpoint: The Azure Document Intelligence resource endpoint URL. Read by the
                composition root from `AZURE_DOCINT_ENDPOINT` (see `config/analyzer.yaml`
                `docint.endpoint_env_var`) — never read here.
            api_key: The Azure Document Intelligence API key. Read by the composition
                root from `AZURE_DOCINT_API_KEY` — never read here.
            api_version: The Document Intelligence REST API version to target.
            model_id: The model to analyze with. Defaults to `"prebuilt-layout"`.
        """
        self._client = DocumentIntelligenceClient(
            endpoint=endpoint,
            credential=AzureKeyCredential(api_key),
            api_version=api_version,
        )
        self._model_id = model_id

    def parse(
        self, envelope: ArrivalEnvelope, bytes_reader: BytesReader
    ) -> list[Element]:
        """See `ParserAdapter.parse`.

        Args:
            envelope: The validated `ArrivalEnvelope`; only `doc_id` is read, for error
                context.
            bytes_reader: Seekable byte source for the scanned PDF's content.

        Returns:
            One `Element` per extracted paragraph (`HEADING` or `PARAGRAPH`) and one
            `Element` per extracted table (`TABLE`, with row/column metadata).

        Raises:
            AnalyzerError: `DOCINT_RATE_LIMITED` for HTTP 429, `DOCINT_UPSTREAM_ERROR`
                for HTTP 5xx. Any other `HttpResponseError` (or any other exception)
                propagates unchanged for `Analyzer` to wrap as `PARSER_INTERNAL`.
        """
        bytes_reader.seek(0)
        data = bytes_reader.read()
        try:
            poller = self._client.begin_analyze_document(
                self._model_id, body=io.BytesIO(data)
            )
            result = poller.result()
        except HttpResponseError as exc:
            mapped_code = _map_status_to_error_code(exc.status_code)
            if mapped_code is None:
                raise
            raise AnalyzerError(
                envelope.doc_id, str(exc), error_code=mapped_code
            ) from exc
        return _elements_from_result(result)


def _map_status_to_error_code(status_code: int | None) -> str | None:
    """Maps an `HttpResponseError.status_code` to a declared Analyzer error code.

    Args:
        status_code: The HTTP status code from the failed Azure Document Intelligence
            call, or `None` if unavailable.

    Returns:
        `DOCINT_RATE_LIMITED` for 429, `DOCINT_UPSTREAM_ERROR` for any 5xx, `None` for
        anything else (the caller lets the original exception propagate unchanged).
    """
    if status_code == _HTTP_TOO_MANY_REQUESTS:
        return DOCINT_RATE_LIMITED
    if status_code is not None and status_code >= _HTTP_SERVER_ERROR_FLOOR:
        return DOCINT_UPSTREAM_ERROR
    return None


def _elements_from_result(result: AnalyzeResult) -> list[Element]:
    """Maps an `AnalyzeResult` to `Element`s: paragraphs, then tables."""
    elements: list[Element] = [
        _paragraph_element(paragraph) for paragraph in result.paragraphs or []
    ]
    elements.extend(_table_element(table) for table in result.tables or [])
    return elements


def _paragraph_element(paragraph: DocumentParagraph) -> Element:
    """Maps one `DocumentParagraph` to a `HEADING` or `PARAGRAPH` Element."""
    element_type = (
        ElementType.HEADING
        if paragraph.role in _HEADING_ROLES
        else ElementType.PARAGRAPH
    )
    return Element(
        type=element_type,
        text=paragraph.content,
        metadata={"role": paragraph.role},
    )


def _table_element(table: DocumentTable) -> Element:
    """Maps one `DocumentTable` to a `TABLE` Element with row/column cell metadata.

    Table serialization strategy (how a chunker should flatten this into text) is out
    of scope for this REQ (story: "belongs to the Chunker") — `text` is a simple
    reading-order join of cell content; `metadata["cells"]` carries the full structured
    row/column data for a downstream chunker to use instead, if preferred.
    """
    cells = [
        {
            "row_index": cell.row_index,
            "column_index": cell.column_index,
            "content": cell.content,
        }
        for cell in table.cells
    ]
    return Element(
        type=ElementType.TABLE,
        text="\n".join(cell.content for cell in table.cells),
        metadata={
            "row_count": table.row_count,
            "column_count": table.column_count,
            "cells": cells,
        },
    )
