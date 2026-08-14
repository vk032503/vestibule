"""TableStorageScenarioStore — Azure Table Storage-backed, writable ScenarioStore
(REQ-010).

Production path. `azure-data-tables` is an optional dependency (the `table_storage`
extra, REQ-008's `azure_search` extra precedent) — lazily imported only inside
`_azure_table_backend._load_real_backend`, so importing this module never hard-fails for
callers who did not install the extra; a missing dependency raises
`SCENARIO_STORE_DEPENDENCY_MISSING` (PERMANENT) at construction time (same fail-fast
shape as `FastEmbedEmbedder`/`AzureAISearchIndexer`).

Partition key = `vertical`, row key = `scenario_id` (story). This adapter talks to the
store only through the small duck-typed `_TableBackend` contract (`get_entity`/`query`/
`upsert_entity`/`delete_entity`) rather than the real SDK's client objects directly, so
every test can inject a recorded-response fake and never needs `azure-data-tables`
installed (`test_azure_ai_search.py` precedent). `_RealTableBackend`
(`_azure_table_backend.py`) is the one production implementation. Entity <-> `Scenario`
conversion lives in `_entity_codec.py`.

Every real-backend call is bounded by an explicit timeout (`_call_with_timeout`, a
`ThreadPoolExecutor`/`Future` seam mirroring `FastEmbedEmbedder`) and retried via
`tenacity` (exponential backoff with full jitter) for HTTP 429/5xx or a timeout —
mapped to `SCENARIO_STORE_UNAVAILABLE` (TRANSIENT) once retries are exhausted. An HTTP
409/412 (a create/update race or an ETag mismatch) is never retried — it maps
immediately to `SCENARIO_CONFLICT` (TRANSIENT), since a blind retry of the exact same
write can never resolve a concurrency conflict (the caller must re-read and retry).

Spec-gap resolution (`config_version` bump algorithm): the story says `config_version`
is "bumped on every edit" but does not specify how. `_bump_config_version` resolves this
as a simple integer counter (stringified): `"1"` for a brand-new scenario, one more than
the previously-stored value otherwise; a non-integer previously-stored value restarts
the counter at `"1"` rather than raising.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol, TypeVar, cast

from azure.core.exceptions import HttpResponseError
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from vestibule.scenario._validation import (
    validate_chunker_bounds,
    validate_dimension_consistency,
    validate_index_name,
)
from vestibule.scenario.model import (
    SCENARIO_CONFLICT,
    SCENARIO_INVALID,
    SCENARIO_STORE_UNAVAILABLE,
    Scenario,
    ScenarioError,
)
from vestibule.scenario.stores._entity_codec import (
    entity_to_scenario,
    is_enabled,
    parse_datetime,
    scenario_to_entity_data,
)
from vestibule.scenario.stores.base import ScenarioStore, select_enabled_for_vertical

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BACKOFF_BASE_SECONDS = 2.0
_DEFAULT_BACKOFF_MAX_SECONDS = 60.0
_CONFLICT_STATUS_CODES = frozenset({409, 412})
_HTTP_TOO_MANY_REQUESTS = 429
_HTTP_SERVER_ERROR_FLOOR = 500

_T = TypeVar("_T")


@dataclass(frozen=True)
class _TableEntityRecord:
    """This adapter's own store-agnostic entity shape (not a real SDK type).

    Attributes:
        partition_key: The entity's `vertical`.
        row_key: The entity's `scenario_id`.
        data: Every other stored property, flat string/bool values.
        etag: The entity's current ETag, for optimistic-concurrency `upsert_entity` calls.
    """

    partition_key: str
    row_key: str
    data: dict[str, Any]
    etag: str


class _BackendTimeout(Exception):
    """Internal sentinel: one backend call attempt exceeded `timeout_seconds`."""


class _TableBackend(Protocol):
    """Minimal duck-typed surface `TableStorageScenarioStore` needs from a table
    backend. Satisfied by `_RealTableBackend` (production) or any test double."""

    def get_entity(self, partition_key: str, row_key: str) -> _TableEntityRecord | None:
        """Returns the entity at `(partition_key, row_key)`, or `None` if absent."""
        ...

    def query(
        self, *, partition_key: str | None = None, row_key: str | None = None
    ) -> list[_TableEntityRecord]:
        """Returns every entity matching the given key filter(s) (`None` = unfiltered)."""
        ...

    def upsert_entity(
        self,
        partition_key: str,
        row_key: str,
        data: dict[str, Any],
        *,
        etag: str | None,
    ) -> str:
        """Creates (`etag=None`) or conditionally updates (`etag` set) one entity."""
        ...

    def delete_entity(self, partition_key: str, row_key: str) -> None:
        """Deletes the entity at `(partition_key, row_key)`; a no-op if absent."""
        ...


class TableStorageScenarioStore(ScenarioStore):
    """Production `ScenarioStore`. Thin wrap of Azure Table Storage.

    `connection_string` is injected by the composition root from env only (house rules:
    no secrets in code); this class never reads env itself (REQ-007/008 precedent).
    """

    def __init__(
        self,
        connection_string: str,
        table_name: str,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS,
        _sleep: Callable[[float], None] = time.sleep,
        _backend: _TableBackend | None = None,
    ) -> None:
        """Initializes the store.

        Args:
            connection_string: The Table Storage connection string. Read by the
                composition root from `AZURE_TABLES_CONNECTION_STRING` — never read here.
            table_name: The table this store reads/writes.
            timeout_seconds: Per-call timeout bounding every real backend call.
            max_attempts: Maximum attempts (first call plus retries) before raising
                `SCENARIO_STORE_UNAVAILABLE`.
            backoff_base_seconds: Exponential-backoff-with-full-jitter multiplier
                between retries.
            backoff_max_seconds: Cap on the computed backoff delay.
            _sleep: Internal test-injection seam for the retry loop's sleep function.
            _backend: Internal test-injection seam standing in for the real Azure Table
                Storage backend; production callers should leave this `None`.

        Raises:
            ScenarioError: `SCENARIO_STORE_DEPENDENCY_MISSING` (PERMANENT) if
                `azure-data-tables` is not installed and `_backend` was not injected.
        """
        self._table_name = table_name
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleep = _sleep
        self._backend = _backend or _load_real_backend_lazily(
            connection_string, table_name
        )

    @property
    def is_writable(self) -> bool:
        """See `ScenarioStore.is_writable`. Always `True`."""
        return True

    def get(self, scenario_id: str) -> Scenario | None:
        """See `ScenarioStore.get`."""
        matches = self._with_retry(self._backend.query, row_key=scenario_id)
        if not matches:
            return None
        return entity_to_scenario(matches[0])

    def get_by_vertical(self, vertical: str) -> Scenario | None:
        """See `ScenarioStore.get_by_vertical`."""
        matches = self._with_retry(self._backend.query, partition_key=vertical)
        candidates = [entity_to_scenario(m) for m in matches if is_enabled(m)]
        return select_enabled_for_vertical(vertical, candidates)

    def list_all(self, include_disabled: bool = False) -> list[Scenario]:
        """See `ScenarioStore.list_all`."""
        matches = self._with_retry(self._backend.query)
        scenarios = [entity_to_scenario(m) for m in matches]
        if include_disabled:
            return scenarios
        return [s for s in scenarios if s.enabled]

    def upsert(self, scenario: Scenario) -> Scenario:
        """See `ScenarioStore.upsert`.

        Raises:
            ScenarioError: `SCENARIO_INVALID` (PERMANENT) if re-validation fails;
                `SCENARIO_CONFLICT` (TRANSIENT) on an ETag/create-race conflict;
                `SCENARIO_STORE_UNAVAILABLE` (TRANSIENT) after exhausting retries.
        """
        _revalidate(scenario)
        existing = self._with_retry(
            self._backend.get_entity, scenario.vertical, scenario.scenario_id
        )
        updated = scenario.model_copy(
            update={
                "config_version": _bump_config_version(
                    existing.data.get("config_version") if existing else None
                ),
                "created_at": (
                    parse_datetime(existing.data["created_at"])
                    if existing is not None
                    else scenario.created_at
                ),
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self._with_retry(
            self._backend.upsert_entity,
            updated.vertical,
            updated.scenario_id,
            scenario_to_entity_data(updated),
            etag=existing.etag if existing is not None else None,
        )
        return updated

    def delete(self, scenario_id: str) -> bool:
        """See `ScenarioStore.delete`."""
        matches = self._with_retry(self._backend.query, row_key=scenario_id)
        if not matches:
            return False
        record = matches[0]
        self._with_retry(
            self._backend.delete_entity, record.partition_key, record.row_key
        )
        return True

    def _with_retry(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Runs one backend call under a timeout, retrying transient failures.

        Raises:
            ScenarioError: `SCENARIO_STORE_UNAVAILABLE` if every attempt timed out or
                returned HTTP 429/5xx; `SCENARIO_CONFLICT` immediately (never retried)
                on HTTP 409/412. Any other exception propagates unchanged.
        """
        retrying: Retrying = Retrying(
            sleep=self._sleep,
            stop=stop_after_attempt(self._max_attempts),
            wait=wait_random_exponential(
                multiplier=self._backoff_base_seconds, max=self._backoff_max_seconds
            ),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )
        try:
            return cast(_T, retrying(self._call_with_timeout, fn, *args, **kwargs))
        except _BackendTimeout as exc:
            raise ScenarioError(
                "", str(exc), error_code=SCENARIO_STORE_UNAVAILABLE
            ) from exc
        except HttpResponseError as exc:
            raise ScenarioError("", str(exc), error_code=_map_status(exc)) from exc

    def _call_with_timeout(
        self, fn: Callable[..., _T], *args: Any, **kwargs: Any
    ) -> _T:
        """Bounds one un-retried backend call attempt by `timeout_seconds`."""
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(fn, *args, **kwargs)
            try:
                return future.result(timeout=self._timeout_seconds)
            except FutureTimeoutError as exc:
                raise _BackendTimeout(
                    f"backend call did not return within {self._timeout_seconds}s"
                ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def _is_retryable(exc: BaseException) -> bool:
    """A timeout or HTTP 429/5xx retries; an HTTP 409/412 conflict never does."""
    if isinstance(exc, _BackendTimeout):
        return True
    if isinstance(exc, HttpResponseError):
        code = exc.status_code
        if code in _CONFLICT_STATUS_CODES:
            return False
        return code == _HTTP_TOO_MANY_REQUESTS or (
            code is not None and code >= _HTTP_SERVER_ERROR_FLOOR
        )
    return False


def _map_status(exc: HttpResponseError) -> str:
    """Maps a terminal (post-retry) `HttpResponseError` to a declared error code."""
    if exc.status_code in _CONFLICT_STATUS_CODES:
        return SCENARIO_CONFLICT
    return SCENARIO_STORE_UNAVAILABLE


def _revalidate(scenario: Scenario) -> None:
    """Re-runs `Scenario`'s own cross-field checks before a write lands (defense in
    depth — `scenario` is already guaranteed valid by its own construction).

    Raises:
        ScenarioError: `SCENARIO_INVALID` (PERMANENT) if any check fails.
    """
    try:
        validate_index_name(scenario.index_name)
        validate_chunker_bounds(scenario.chunker)
        validate_dimension_consistency(scenario.embedder, scenario.indexer)
    except ValueError as exc:
        raise ScenarioError(
            scenario.scenario_id, str(exc), error_code=SCENARIO_INVALID
        ) from exc


def _bump_config_version(existing: str | None) -> str:
    """See this module's docstring for the bump algorithm and its spec-gap resolution."""
    if existing is None:
        return "1"
    try:
        return str(int(existing) + 1)
    except ValueError:
        return "1"


def _load_real_backend_lazily(connection_string: str, table_name: str) -> _TableBackend:
    """Defers to `_azure_table_backend._load_real_backend` via a call-time (not
    module-level) import, breaking what would otherwise be a circular import: that
    module imports `_TableEntityRecord`/`_TableBackend` back from this one.

    Raises:
        ScenarioError: `SCENARIO_STORE_DEPENDENCY_MISSING` (PERMANENT) if
            `azure-data-tables` is not installed. See
            `vestibule.scenario.stores._azure_table_backend`.
    """
    from vestibule.scenario.stores._azure_table_backend import _load_real_backend

    return _load_real_backend(connection_string, table_name)
