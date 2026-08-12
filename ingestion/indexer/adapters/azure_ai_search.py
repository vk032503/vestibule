"""AzureAISearchIndexer — thin IndexerAdapter wrapping Azure AI Search (REQ-008).

Production path. `azure-search-documents` is an optional dependency (the
`azure_search` extra, REQ-008 precedent: REQ-007's `local` extra for `fastembed`) —
lazily imported inside `__init__` so importing this module never hard-fails for callers
who did not install the extra; a missing dependency raises `INDEXER_DEPENDENCY_MISSING`
(PERMANENT) at construction time (same fail-fast shape as `FastEmbedEmbedder`).

This class talks to the store only through a small internal duck-typed contract
(`get_index_schema`/`create_or_update_index`/`upload_documents`/`delete_documents`/
`documents_for_doc_id`, all bound to one `_backend` seam) rather than the real SDK's
client objects directly, so every test can inject a recorded-response fake implementing
that same contract and never needs `azure-search-documents` installed — the exact
`test_azure_openai.py`/`test_fastembed.py` precedent this REQ's story calls out.
`_RealSearchBackend` (this module) is the one production implementation, translating
this adapter's calls to the real SDK; its own translation logic is inherently untestable
hermetically (no live Azure calls in CI) and is kept intentionally thin.

Every real-backend call is bounded by an explicit timeout (`_call_with_timeout`, a
`ThreadPoolExecutor`/`Future` seam, mirroring `FastEmbedEmbedder`'s model-load timeout —
the SDK exposes no reliable per-call timeout kwarg across all of its methods) and
retried via `tenacity` (exponential backoff with full jitter, honoring an
`HttpResponseError`'s `Retry-After` header when present), mirroring
`AzureOpenAIEmbedder`.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeVar, cast

from azure.core.exceptions import HttpResponseError
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from ingestion.indexer.adapters.base import IndexerAdapter, UpsertOutcome
from ingestion.indexer.model import (
    INDEXER_RATE_LIMITED,
    INDEXER_RECORD_TOO_LARGE,
    INDEXER_SCHEMA_CONFLICT,
    INDEXER_TIMEOUT,
    INDEXER_UPSTREAM_ERROR,
    IndexerError,
    IndexRecord,
)

_MAX_BATCH_SIZE = 100
_SUPPORTS_HYBRID = True
_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BACKOFF_BASE_SECONDS = 2.0
_DEFAULT_BACKOFF_MAX_SECONDS = 60.0
_MAX_RECORD_BYTES = 16 * 1024 * 1024  # Azure AI Search's per-document size ceiling.
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_FLOOR = 500

_T = TypeVar("_T")


@dataclass(frozen=True)
class _IndexDefinition:
    """This adapter's own store-agnostic index schema request (not a real SDK type)."""

    index_name: str
    dimensions: int
    metric: str
    hnsw_m: int
    hnsw_ef_construction: int
    hnsw_ef_search: int


class _BackendTimeout(Exception):
    """Internal sentinel: one backend call attempt exceeded `timeout_seconds`."""


def _is_retryable(exc: BaseException) -> bool:
    """Whether `exc` should trigger a `tenacity` retry.

    A timeout always retries; an `HttpResponseError` retries only when its status code
    maps to a declared TRANSIENT code (429/5xx) — an unmapped `HttpResponseError` (e.g.
    a genuine 400) is never retried, mirroring `AzureOpenAIEmbedder`'s precedent of only
    retrying exception types that already represent 429/5xx/timeout.

    Args:
        exc: The exception raised by the most recent attempt.

    Returns:
        `True` if `exc` should trigger a retry; `False` otherwise.
    """
    if isinstance(exc, _BackendTimeout):
        return True
    if isinstance(exc, HttpResponseError):
        return _map_status_to_error_code(exc.status_code) is not None
    return False


class _SearchBackend(Protocol):
    """Minimal duck-typed surface `AzureAISearchIndexer` needs from a search backend.

    Satisfied by `_RealSearchBackend` (production) or any test double implementing the
    same five methods (tests).
    """

    def get_index_schema(self, index_name: str) -> tuple[int, str] | None:
        """Returns `(dimensions, metric)` of the existing index, or `None` if absent."""
        ...

    def create_or_update_index(self, definition: _IndexDefinition) -> None:
        """Creates or updates the index to match `definition`."""
        ...

    def upload_documents(
        self, index_name: str, documents: list[dict[str, Any]]
    ) -> list[tuple[str, bool, str | None]]:
        """Upserts `documents`; returns `(chunk_id, succeeded, error_message)` per item."""
        ...

    def delete_documents(self, index_name: str, chunk_ids: list[str]) -> None:
        """Deletes the documents keyed by `chunk_ids`."""
        ...

    def documents_for_doc_id(
        self, index_name: str, doc_id: str
    ) -> list[dict[str, Any]]:
        """Returns every stored document (`chunk_id`, `doc_version`) for `doc_id`."""
        ...


class AzureAISearchIndexer(IndexerAdapter):
    """Production `IndexerAdapter`. Thin wrap of Azure AI Search.

    `endpoint`/`api_key` are injected by the composition root from env only (house
    rules: no secrets in code); this class never reads env itself (REQ-005/007
    precedent).
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        index_name: str,
        *,
        hnsw_m: int = 4,
        hnsw_ef_construction: int = 400,
        hnsw_ef_search: int = 500,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS,
        _sleep: Callable[[float], None] = time.sleep,
        _backend: _SearchBackend | None = None,
    ) -> None:
        """Initializes the adapter.

        Args:
            endpoint: The Azure AI Search resource endpoint URL. Read by the
                composition root from `AZURE_SEARCH_ENDPOINT` — never read here.
            api_key: The Azure AI Search admin API key. Read by the composition root
                from `AZURE_SEARCH_API_KEY` — never read here.
            index_name: The index this adapter reads/writes.
            hnsw_m: HNSW `m` parameter passed to `ensure_schema`'s index creation.
            hnsw_ef_construction: HNSW `ef_construction` parameter.
            hnsw_ef_search: HNSW `ef_search` parameter.
            timeout_seconds: Per-call timeout bounding every real backend call.
            max_attempts: Maximum attempts (first call plus retries) before raising a
                TRANSIENT `IndexerError`.
            backoff_base_seconds: Exponential-backoff-with-full-jitter multiplier
                between retries, when no `Retry-After` header is present.
            backoff_max_seconds: Cap on the computed backoff delay.
            _sleep: Internal test-injection seam for the retry loop's sleep function.
            _backend: Internal test-injection seam standing in for the real Azure AI
                Search backend; production callers should leave this `None` (a real
                `_RealSearchBackend` is constructed instead, lazily importing
                `azure-search-documents`).

        Raises:
            IndexerError: `INDEXER_DEPENDENCY_MISSING` (PERMANENT) if
                `azure-search-documents` is not installed and `_backend` was not
                injected.
        """
        self._index_name = index_name
        self._hnsw_m = hnsw_m
        self._hnsw_ef_construction = hnsw_ef_construction
        self._hnsw_ef_search = hnsw_ef_search
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleep = _sleep
        self._backend = _backend or _load_real_backend_lazily(endpoint, api_key)

    @property
    def index_name(self) -> str:
        """See `IndexerAdapter.index_name`."""
        return self._index_name

    @property
    def max_batch_size(self) -> int:
        """See `IndexerAdapter.max_batch_size`."""
        return _MAX_BATCH_SIZE

    @property
    def supports_hybrid(self) -> bool:
        """See `IndexerAdapter.supports_hybrid`. Always `True` (schema-supported, not
        wired — story: "Out of scope")."""
        return _SUPPORTS_HYBRID

    def ensure_schema(self, dimensions: int, metric: str) -> None:
        """See `IndexerAdapter.ensure_schema`."""
        existing = self._with_retry(self._backend.get_index_schema, self._index_name)
        if existing is None:
            definition = _IndexDefinition(
                index_name=self._index_name,
                dimensions=dimensions,
                metric=metric,
                hnsw_m=self._hnsw_m,
                hnsw_ef_construction=self._hnsw_ef_construction,
                hnsw_ef_search=self._hnsw_ef_search,
            )
            self._with_retry(self._backend.create_or_update_index, definition)
            return
        existing_dimensions, existing_metric = existing
        if existing_dimensions != dimensions or existing_metric != metric:
            raise IndexerError(
                "",
                f"index {self._index_name!r} exists with dimensions="
                f"{existing_dimensions} metric={existing_metric!r}; requested "
                f"dimensions={dimensions} metric={metric!r}",
                error_code=INDEXER_SCHEMA_CONFLICT,
            )
        # Already compatible — idempotent no-op (AC8).

    def upsert(self, records: list[IndexRecord]) -> UpsertOutcome:
        """See `IndexerAdapter.upsert`.

        Checks every record's serialized size against `_MAX_RECORD_BYTES` before
        sending anything (a whole-call precondition, like `Indexer`'s own
        dimension/mixed-model checks) — raises `INDEXER_RECORD_TOO_LARGE` (PERMANENT)
        before any write if any record is oversized.
        """
        documents = [_record_to_document(record) for record in records]
        _check_record_sizes(records, documents)
        results = self._with_retry(
            self._backend.upload_documents, self._index_name, documents
        )
        succeeded = [chunk_id for chunk_id, ok, _msg in results if ok]
        failed = [
            (chunk_id, msg or "upload failed")
            for chunk_id, ok, msg in results
            if not ok
        ]
        return UpsertOutcome(succeeded=succeeded, failed=failed)

    def delete_by_doc_id(self, doc_id: str) -> int:
        """See `IndexerAdapter.delete_by_doc_id`."""
        documents = self._with_retry(
            self._backend.documents_for_doc_id, self._index_name, doc_id
        )
        if not documents:
            return 0
        chunk_ids = [str(doc["chunk_id"]) for doc in documents]
        self._with_retry(self._backend.delete_documents, self._index_name, chunk_ids)
        return len(chunk_ids)

    def count_by_doc_id(self, doc_id: str) -> int:
        """See `IndexerAdapter.count_by_doc_id`."""
        documents = self._with_retry(
            self._backend.documents_for_doc_id, self._index_name, doc_id
        )
        return len(documents)

    def get_doc_version(self, doc_id: str) -> str | None:
        """See `IndexerAdapter.get_doc_version`."""
        documents = self._with_retry(
            self._backend.documents_for_doc_id, self._index_name, doc_id
        )
        if not documents:
            return None
        return str(documents[0].get("doc_version"))

    def _with_retry(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Runs one backend call under a timeout, retrying transient failures.

        Args:
            fn: The `_SearchBackend` method to call.
            *args: Positional arguments forwarded to `fn`.

        Returns:
            `fn(*args)`'s result.

        Raises:
            IndexerError: `INDEXER_TIMEOUT` if every attempt exceeded
                `timeout_seconds`; `INDEXER_RATE_LIMITED`/`INDEXER_UPSTREAM_ERROR` if
                every attempt raised a mapped `HttpResponseError`. Any other
                `HttpResponseError` (or any other exception) propagates unchanged for
                `Indexer` to wrap as `INDEXER_INTERNAL`.
        """
        retrying: Retrying = Retrying(
            sleep=self._sleep,
            stop=stop_after_attempt(self._max_attempts),
            wait=_RetryAfterAwareWait(
                self._backoff_base_seconds, self._backoff_max_seconds
            ),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        try:
            return cast(_T, retrying(self._call_with_timeout, fn, *args))
        except _BackendTimeout as exc:
            raise IndexerError("", str(exc), error_code=INDEXER_TIMEOUT) from exc
        except HttpResponseError as exc:
            mapped_code = _map_status_to_error_code(exc.status_code)
            if mapped_code is None:
                raise
            raise IndexerError("", str(exc), error_code=mapped_code) from exc

    def _call_with_timeout(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Bounds one un-retried backend call attempt by `timeout_seconds`.

        Args:
            fn: The `_SearchBackend` method to call.
            *args: Positional arguments forwarded to `fn`.

        Returns:
            `fn(*args)`'s result.

        Raises:
            _BackendTimeout: If `fn(*args)` does not return within `timeout_seconds`.
        """
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, *args)
            try:
                return future.result(timeout=self._timeout_seconds)
            except FutureTimeoutError as exc:
                raise _BackendTimeout(
                    f"backend call did not return within {self._timeout_seconds}s"
                ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


class _RetryAfterAwareWait:
    """`tenacity` wait strategy honoring an `HttpResponseError`'s `Retry-After` header.

    Falls back to exponential backoff with full jitter for every other retryable
    exception, or when a 429 carries no usable `Retry-After` (`AzureOpenAIEmbedder`
    precedent).
    """

    def __init__(self, base_seconds: float, max_seconds: float) -> None:
        """Initializes the fallback backoff strategy.

        Args:
            base_seconds: The fallback strategy's exponential multiplier.
            max_seconds: The fallback strategy's cap on the computed delay.
        """
        self._fallback = wait_random_exponential(
            multiplier=base_seconds, max=max_seconds
        )

    def __call__(self, retry_state: RetryCallState) -> float:
        """Computes the delay before the next retry attempt.

        Args:
            retry_state: The current `tenacity` retry state.

        Returns:
            The `Retry-After` value in seconds if `retry_state`'s outcome is an
            `HttpResponseError` carrying a parseable `Retry-After` header; otherwise
            the fallback exponential-backoff-with-jitter delay.
        """
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, HttpResponseError):
            retry_after = _parse_retry_after(exc)
            if retry_after is not None:
                return retry_after
        return float(self._fallback(retry_state))


def _parse_retry_after(exc: HttpResponseError) -> float | None:
    """Parses an `HttpResponseError`'s `Retry-After` header, if present and well-formed.

    Args:
        exc: The `HttpResponseError` to inspect.

    Returns:
        The header's value in seconds, or `None` if absent, unparseable, or the
        exception carries no response.
    """
    response = exc.response
    if response is None:
        return None
    # `_HttpResponseCommonAPI` (the type of `HttpResponseError.response`) declares no
    # `headers` attribute in azure-core's own stubs, even though every real HTTP
    # response object azure-core produces has one — the same gap `AzureOpenAIEmbedder`
    # sidesteps via `openai`'s own concretely-typed `response`, unavailable here.
    headers = cast(Any, response).headers
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _map_status_to_error_code(status_code: int | None) -> str | None:
    """Maps an `HttpResponseError.status_code` to a declared Indexer error code.

    Args:
        status_code: The HTTP status code from the failed call, or `None`.

    Returns:
        `INDEXER_RATE_LIMITED` for 429, `INDEXER_UPSTREAM_ERROR` for any 5xx, `None`
        for anything else (the caller lets the original exception propagate).
    """
    if status_code == _HTTP_TOO_MANY_REQUESTS:
        return INDEXER_RATE_LIMITED
    if status_code is not None and status_code >= _HTTP_SERVER_ERROR_FLOOR:
        return INDEXER_UPSTREAM_ERROR
    return None


def _record_to_document(record: IndexRecord) -> dict[str, Any]:
    """Maps one `IndexRecord` to the flat document shape Azure AI Search expects.

    Args:
        record: The record to convert.

    Returns:
        A JSON-serializable document, keyed by `chunk_id`.
    """
    return {
        "chunk_id": record.chunk_id,
        "doc_id": record.doc_id,
        "doc_version": record.doc_version,
        "content": record.text,
        "content_vector": record.vector,
        "position": record.position,
        "section_path": record.section_path,
        "element_types": record.element_types,
        "strategy": record.strategy,
        "allowed_groups": record.allowed_groups,
        "trust_tier": record.trust_tier,
        "embedding_model": record.embedding_model,
        "embedding_dimensions": record.embedding_dimensions,
        "config_version": record.config_version,
        "indexed_at": record.indexed_at.isoformat(),
    }


def _check_record_sizes(
    records: list[IndexRecord], documents: list[dict[str, Any]]
) -> None:
    """Rejects the whole batch if any single document exceeds `_MAX_RECORD_BYTES`.

    Args:
        records: The source records, for `chunk_id` in the raised error message.
        documents: `records` already converted via `_record_to_document`, in order.

    Raises:
        IndexerError: `INDEXER_RECORD_TOO_LARGE` (PERMANENT) on the first oversized
            record found, before any write.
    """
    for record, document in zip(records, documents, strict=True):
        size = len(json.dumps(document).encode("utf-8"))
        if size > _MAX_RECORD_BYTES:
            raise IndexerError(
                record.doc_id,
                f"record chunk_id={record.chunk_id} serialized size={size} bytes "
                f"exceeds the store's {_MAX_RECORD_BYTES}-byte limit",
                error_code=INDEXER_RECORD_TOO_LARGE,
            )


def _load_real_backend_lazily(endpoint: str, api_key: str) -> _SearchBackend:
    """Defers to `_azure_search_backend._load_real_backend` via a call-time (not
    module-level) import, breaking what would otherwise be a circular import: that
    module imports `_IndexDefinition`/`_SearchBackend` back from this one.

    Args:
        endpoint: The Azure AI Search resource endpoint URL.
        api_key: The Azure AI Search admin API key.

    Returns:
        A real `_SearchBackend` implementation.

    Raises:
        IndexerError: `INDEXER_DEPENDENCY_MISSING` (PERMANENT) if
            `azure-search-documents` is not installed. See
            `ingestion.indexer.adapters._azure_search_backend`.
    """
    from ingestion.indexer.adapters._azure_search_backend import _load_real_backend

    return _load_real_backend(endpoint, api_key)
