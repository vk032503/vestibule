"""AzureAISearchProvisioner — thin IndexProvisionerAdapter wrapping Azure AI Search
(REQ-011).

Mirrors `AzureAISearchIndexer`'s shape exactly (REQ-008): duck-typed
`_ProvisionerBackend` seam, lazy `azure-search-documents` import, `tenacity` retry +
`_call_with_timeout`, Retry-After-aware backoff.
"""

from __future__ import annotations

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

from vestibule.provisioning.adapters.base import IndexProvisionerAdapter
from vestibule.provisioning.model import (
    INDEX_PROVISIONER_UNAVAILABLE,
    FieldSpec,
    IndexDescription,
    IndexTemplate,
    ProvisioningError,
)

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BACKOFF_BASE_SECONDS = 2.0
_DEFAULT_BACKOFF_MAX_SECONDS = 60.0
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
    fields: tuple[FieldSpec, ...]
    semantic_ranker_enabled: bool


class _BackendTimeout(Exception):
    """Internal sentinel: one backend call attempt exceeded `timeout_seconds`."""


def _is_retryable(exc: BaseException) -> bool:
    """Whether `exc` should trigger a `tenacity` retry.

    A timeout always retries; an `HttpResponseError` retries only when its status code
    maps to a declared TRANSIENT code (429/5xx).
    """
    if isinstance(exc, _BackendTimeout):
        return True
    if isinstance(exc, HttpResponseError):
        return _is_transient_status(exc.status_code)
    return False


def _is_transient_status(status_code: int | None) -> bool:
    """Whether `status_code` maps to a declared TRANSIENT retry condition."""
    return status_code == _HTTP_TOO_MANY_REQUESTS or (
        status_code is not None and status_code >= _HTTP_SERVER_ERROR_FLOOR
    )


class _ProvisionerBackend(Protocol):
    """Minimal duck-typed surface `AzureAISearchProvisioner` needs from a search
    backend. Satisfied by `_RealSearchBackend` (production) or any test double."""

    def get_index_schema(self, index_name: str) -> tuple[int, str] | None:
        """Returns `(dimensions, metric)` of the existing index, or `None` if absent
        or the index has no discoverable vector field."""
        ...

    def create_or_update_index(self, definition: _IndexDefinition) -> None:
        """Creates or updates the index to match `definition` (idempotent, Assumption
        A13)."""
        ...

    def delete_index(self, index_name: str) -> None:
        """Deletes `index_name`; a no-op if it does not exist."""
        ...


class AzureAISearchProvisioner(IndexProvisionerAdapter):
    """Production `IndexProvisionerAdapter`. Thin wrap of Azure AI Search.

    `endpoint`/`api_key` are injected by the composition root from env only (house
    rules: no secrets in code); this class never reads env itself.
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS,
        _sleep: Callable[[float], None] = time.sleep,
        _backend: _ProvisionerBackend | None = None,
    ) -> None:
        """Initializes the adapter.

        Args:
            endpoint: The Azure AI Search resource endpoint URL. Read by the
                composition root from `AZURE_SEARCH_ENDPOINT` — never read here.
            api_key: The Azure AI Search admin API key. Read by the composition root
                from `AZURE_SEARCH_API_KEY` — never read here.
            timeout_seconds: Per-call timeout bounding every real backend call.
            max_attempts: Maximum attempts (first call plus retries) before raising a
                TRANSIENT `ProvisioningError`.
            backoff_base_seconds: Exponential-backoff-with-full-jitter multiplier
                between retries, when no `Retry-After` header is present.
            backoff_max_seconds: Cap on the computed backoff delay.
            _sleep: Internal test-injection seam for the retry loop's sleep function.
            _backend: Internal test-injection seam standing in for the real Azure AI
                Search backend; production callers should leave this `None`.

        Raises:
            ProvisioningError: `INDEX_PROVISIONER_DEPENDENCY_MISSING` (PERMANENT) if
                `azure-search-documents` is not installed and `_backend` was not
                injected.
        """
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleep = _sleep
        self._backend = _backend or _load_real_backend_lazily(endpoint, api_key)

    def index_exists(self, index_name: str) -> bool:
        """See `IndexProvisionerAdapter.index_exists`."""
        return self._with_retry(self._backend.get_index_schema, index_name) is not None

    def create_index(
        self, index_name: str, template: IndexTemplate, dimensions: int, metric: str
    ) -> None:
        """See `IndexProvisionerAdapter.create_index`.

        Translates `template.fields`/`.hnsw`/`.semantic_ranker_enabled` into a real
        `SearchIndex` (generalized from REQ-008's fixed 8-field list to
        `template.fields`; adds a semantic-search config iff
        `semantic_ranker_enabled`). `hybrid_enabled` has no schema effect
        (`IndexTemplate`'s own docstring). Satisfies Assumption A13's idempotent
        create-or-update contract by construction: the underlying backend call
        dispatches to the real SDK's `SearchIndexClient.create_or_update_index`, an
        inherently idempotent create-or-update primitive, never a create-only one.
        """
        definition = _IndexDefinition(
            index_name=index_name,
            dimensions=dimensions,
            metric=metric,
            hnsw_m=template.hnsw.m,
            hnsw_ef_construction=template.hnsw.ef_construction,
            hnsw_ef_search=template.hnsw.ef_search,
            fields=tuple(template.fields),
            semantic_ranker_enabled=template.semantic_ranker_enabled,
        )
        self._with_retry(self._backend.create_or_update_index, definition)

    def describe_index(self, index_name: str) -> IndexDescription | None:
        """See `IndexProvisionerAdapter.describe_index`."""
        schema = self._with_retry(self._backend.get_index_schema, index_name)
        if schema is None:
            return None
        dimensions, metric = schema
        return IndexDescription(dimensions=dimensions, metric=metric)

    def delete_index(self, index_name: str) -> None:
        """See `IndexProvisionerAdapter.delete_index`."""
        self._with_retry(self._backend.delete_index, index_name)

    def _with_retry(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Runs one backend call under a timeout, retrying transient failures.

        Raises:
            ProvisioningError: `INDEX_PROVISIONER_UNAVAILABLE` (TRANSIENT) if every
                attempt timed out or returned a mapped HTTP 429/5xx. Any other
                exception (including an unmapped `HttpResponseError`) propagates
                unchanged for `IndexProvisioner` to wrap per Assumption A7.
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
            raise ProvisioningError(
                "", str(exc), error_code=INDEX_PROVISIONER_UNAVAILABLE
            ) from exc
        except HttpResponseError as exc:
            if not _is_transient_status(exc.status_code):
                raise
            raise ProvisioningError(
                "", str(exc), error_code=INDEX_PROVISIONER_UNAVAILABLE
            ) from exc

    def _call_with_timeout(self, fn: Callable[..., _T], *args: Any) -> _T:
        """Bounds one un-retried backend call attempt by `timeout_seconds`."""
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
    exception, or when a 429 carries no usable `Retry-After`.
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
    """Parses an `HttpResponseError`'s `Retry-After` header, if present and well-formed."""
    response = exc.response
    if response is None:
        return None
    headers = cast(Any, response).headers
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _load_real_backend_lazily(endpoint: str, api_key: str) -> _ProvisionerBackend:
    """Defers to `_azure_search_backend._load_real_backend` via a call-time (not
    module-level) import, breaking what would otherwise be a circular import: that
    module imports `_IndexDefinition`/`_ProvisionerBackend` back from this one.

    Raises:
        ProvisioningError: `INDEX_PROVISIONER_DEPENDENCY_MISSING` (PERMANENT) if
            `azure-search-documents` is not installed. See
            `vestibule.provisioning.adapters._azure_search_backend`.
    """
    from vestibule.provisioning.adapters._azure_search_backend import _load_real_backend

    return _load_real_backend(endpoint, api_key)
