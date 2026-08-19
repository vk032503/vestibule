"""TableStorageIndexRegistry — Azure Table Storage-backed IndexRegistry (REQ-011).

Production path; the only backend that provides genuine cross-process guarantees
(Contract #2 touchpoint). Mirrors `TableStorageScenarioStore`'s shape and
retry/timeout machinery (`_with_retry`/`_call_with_timeout`, `tenacity`,
`azure-data-tables` lazy import -> `INDEX_PROVISIONER_DEPENDENCY_MISSING`) — reusing
the *pattern* established there, not its private types (Assumption A9): this module
defines its own `_TableBackend`/`_TableEntityRecord`, independent of
`vestibule.scenario.stores`.

Partition key = `vertical`, row key = `index_name`.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Protocol, TypeVar, cast

from azure.core.exceptions import HttpResponseError
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from vestibule.provisioning.model import (
    INDEX_PROVISION_CONFLICT,
    INDEX_REGISTRY_UNAVAILABLE,
    IndexRegistryEntry,
    ProvisioningError,
)
from vestibule.provisioning.registry import IndexRegistry
from vestibule.provisioning.stores._entity_codec import (
    entity_to_entry,
    entry_to_entity_data,
)

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
        row_key: The entity's `index_name`.
        data: Every other stored property, flat string/int/bool values.
        etag: The entity's current ETag, for optimistic-concurrency `upsert_entity`
            calls.
    """

    partition_key: str
    row_key: str
    data: dict[str, Any]
    etag: str


class _BackendTimeout(Exception):
    """Internal sentinel: one backend call attempt exceeded `timeout_seconds`."""


class _TableBackend(Protocol):
    """Minimal duck-typed surface `TableStorageIndexRegistry` needs from a table
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


class TableStorageIndexRegistry(IndexRegistry):
    """Production `IndexRegistry`. Thin wrap of Azure Table Storage.

    `connection_string` is injected by the composition root from env only (house
    rules: no secrets in code); this class never reads env itself.
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
        """Initializes the registry.

        Args:
            connection_string: The Table Storage connection string. Read by the
                composition root from `AZURE_TABLES_CONNECTION_STRING` — never read
                here.
            table_name: The table this registry reads/writes.
            timeout_seconds: Per-call timeout bounding every real backend call.
            max_attempts: Maximum attempts (first call plus retries) before raising
                `INDEX_REGISTRY_UNAVAILABLE`.
            backoff_base_seconds: Exponential-backoff-with-full-jitter multiplier
                between retries.
            backoff_max_seconds: Cap on the computed backoff delay.
            _sleep: Internal test-injection seam for the retry loop's sleep function.
            _backend: Internal test-injection seam standing in for the real Azure
                Table Storage backend; production callers should leave this `None`.

        Raises:
            ProvisioningError: `INDEX_PROVISIONER_DEPENDENCY_MISSING` (PERMANENT) if
                `azure-data-tables` is not installed and `_backend` was not injected —
                this REQ's one dependency-missing code covers both this registry
                backend and the Azure AI Search provisioner adapter (Assumption).
        """
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleep = _sleep
        self._backend = _backend or _load_real_backend_lazily(
            connection_string, table_name
        )

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        """See `IndexRegistry.get`."""
        matches = self._with_retry(self._backend.query, row_key=index_name)
        if not matches:
            return None
        return entity_to_entry(matches[0])

    def list_by_vertical(self, vertical: str) -> list[IndexRegistryEntry]:
        """See `IndexRegistry.list_by_vertical`."""
        matches = self._with_retry(self._backend.query, partition_key=vertical)
        return [entity_to_entry(m) for m in matches]

    def register(self, entry: IndexRegistryEntry) -> IndexRegistryEntry:
        """See `IndexRegistry.register`.

        "Atomic" means: dispatches to the real SDK's unconditional-create call, which
        Azure Table Storage itself guarantees fails with HTTP 409 if an entity with
        that exact `(PartitionKey, RowKey)` already exists, server-side, across every
        process/region. A 409 here is caught specifically and falls back to a fresh
        `get_entity` read, returning the now-existing entity — never propagated as an
        error (the story's "the losing worker does not error").
        """
        try:
            self._with_retry(
                self._backend.upsert_entity,
                entry.vertical,
                entry.index_name,
                entry_to_entity_data(entry),
                etag=None,
            )
        except ProvisioningError as exc:
            if exc.error_code != INDEX_PROVISION_CONFLICT:
                raise
            record = self._with_retry(
                self._backend.get_entity, entry.vertical, entry.index_name
            )
            if record is None:
                raise
            return entity_to_entry(record)
        return entry

    def reclaim(
        self,
        index_name: str,
        observed: IndexRegistryEntry,
        new_entry: IndexRegistryEntry,
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.reclaim`.

        A fresh `get_entity` read obtains the entity's current real ETag. The
        client-side "is it still equal to `observed`" comparison below is a cheap
        early exit only — correctness comes entirely from the subsequent
        ETag-conditional `upsert_entity` call, which Azure Table Storage itself
        guarantees only lands if the entity's ETag, server-side, still equals the one
        just read.
        """
        record = self._with_retry(
            self._backend.get_entity, observed.vertical, index_name
        )
        if record is None or entity_to_entry(record) != observed:
            raise ProvisioningError(
                index_name,
                "reclaim observed a stale snapshot; the stored entry has since changed",
                error_code=INDEX_PROVISION_CONFLICT,
            )
        self._with_retry(
            self._backend.upsert_entity,
            new_entry.vertical,
            index_name,
            entry_to_entity_data(new_entry),
            etag=record.etag,
        )
        return new_entry

    def touch_verified(self, index_name: str) -> IndexRegistryEntry:
        """See `IndexRegistry.touch_verified`."""
        record = self._require_record(index_name)
        entry = entity_to_entry(record)
        updated = entry.model_copy(update={"last_verified_at": _now()})
        self._with_retry(
            self._backend.upsert_entity,
            entry.vertical,
            index_name,
            entry_to_entity_data(updated),
            etag=record.etag,
        )
        return updated

    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_ready`."""
        record = self._require_record(index_name)
        entry = entity_to_entry(record)
        _check_claim_token(entry, expected_claim_token)
        updated = entry.model_copy(
            update={
                "status": "ready",
                "last_verified_at": _now(),
                "claim_token": None,
                "claimed_at": None,
                "resolved_template": None,
            }
        )
        self._with_retry(
            self._backend.upsert_entity,
            entry.vertical,
            index_name,
            entry_to_entity_data(updated),
            etag=record.etag,
        )
        return updated

    def mark_failed(
        self, index_name: str, reason: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_failed`."""
        record = self._require_record(index_name)
        entry = entity_to_entry(record)
        _check_claim_token(entry, expected_claim_token)
        updated = entry.model_copy(
            update={
                "status": "failed",
                "last_error_message": reason,
                "claim_token": None,
                "claimed_at": None,
                "resolved_template": None,
            }
        )
        self._with_retry(
            self._backend.upsert_entity,
            entry.vertical,
            index_name,
            entry_to_entity_data(updated),
            etag=record.etag,
        )
        return updated

    def mark_retired(self, index_name: str) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_retired`."""
        record = self._require_record(index_name)
        entry = entity_to_entry(record)
        updated = entry.model_copy(update={"status": "retired"})
        self._with_retry(
            self._backend.upsert_entity,
            entry.vertical,
            index_name,
            entry_to_entity_data(updated),
            etag=record.etag,
        )
        return updated

    def _require_record(self, index_name: str) -> _TableEntityRecord:
        """Returns the live entity for `index_name`.

        Raises:
            ProvisioningError: `INDEX_PROVISION_CONFLICT` if no entity exists — see
                `vestibule.provisioning.registry`'s module docstring for the identical
                spec-gap resolution this mirrors.
        """
        matches = self._with_retry(self._backend.query, row_key=index_name)
        if not matches:
            raise ProvisioningError(
                index_name,
                "no registry entry exists for index_name",
                error_code=INDEX_PROVISION_CONFLICT,
            )
        return matches[0]

    def _with_retry(self, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Runs one backend call under a timeout, retrying transient failures.

        Raises:
            ProvisioningError: `INDEX_REGISTRY_UNAVAILABLE` if every attempt timed out
                or returned HTTP 429/5xx; `INDEX_PROVISION_CONFLICT` immediately (never
                retried) on HTTP 409/412. Any other exception propagates unchanged.
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
            raise ProvisioningError(
                "", str(exc), error_code=INDEX_REGISTRY_UNAVAILABLE
            ) from exc
        except HttpResponseError as exc:
            raise ProvisioningError("", str(exc), error_code=_map_status(exc)) from exc

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


def _now() -> Any:
    """Lazily imports `datetime`/`timezone` to avoid a module-level import purely for
    this one call site — kept local since every other timestamp in this module is
    already a stored/parsed value, not a fresh one."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _check_claim_token(
    entry: IndexRegistryEntry, expected_claim_token: str | None
) -> None:
    """Raises `INDEX_PROVISION_CONFLICT` if `expected_claim_token` no longer matches.

    Args:
        entry: The entry currently stored (freshly read).
        expected_claim_token: The caller's own claim token, or `None` to skip the
            check (Assumption A5).

    Raises:
        ProvisioningError: `INDEX_PROVISION_CONFLICT` (TRANSIENT) if
            `expected_claim_token` is non-`None` and does not match
            `entry.claim_token`.
    """
    if expected_claim_token is not None and entry.claim_token != expected_claim_token:
        raise ProvisioningError(
            entry.index_name,
            "claim_token no longer matches the stored entry (superseded by a "
            "reclaim or a concurrent finalization)",
            error_code=INDEX_PROVISION_CONFLICT,
        )


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
        return INDEX_PROVISION_CONFLICT
    return INDEX_REGISTRY_UNAVAILABLE


def _load_real_backend_lazily(connection_string: str, table_name: str) -> _TableBackend:
    """Defers to `_azure_table_backend._load_real_backend` via a call-time (not
    module-level) import, breaking what would otherwise be a circular import: that
    module imports `_TableEntityRecord`/`_TableBackend` back from this one.

    Raises:
        ProvisioningError: `INDEX_PROVISIONER_DEPENDENCY_MISSING` (PERMANENT) if
            `azure-data-tables` is not installed. See
            `vestibule.provisioning.stores._azure_table_backend`.
    """
    from vestibule.provisioning.stores._azure_table_backend import _load_real_backend

    return _load_real_backend(connection_string, table_name)
