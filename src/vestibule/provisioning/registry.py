"""IndexRegistry port + InMemoryIndexRegistry (REQ-011).

See `docs/designs/REQ-011-lld.md` §3.3/§3.4 for the full concurrency reasoning behind
`register()`/`reclaim()`/`mark_ready()`/`mark_failed()`'s exact semantics — this is the
load-bearing module of this REQ.

Spec-gap resolution (unknown `index_name` on a mutating call other than `register`):
none of `reclaim`/`touch_verified`/`mark_ready`/`mark_failed`/`mark_retired` are ever
called by `IndexProvisioner` itself against an `index_name` with no existing registry
entry (every call site first observed an entry via `get()`/`register()`). A direct call
against an unknown `index_name` is therefore a caller bug, not a real race — resolved
here the same way a genuine CAS mismatch is resolved: raising
`ProvisioningError(INDEX_PROVISION_CONFLICT)` (the closest declared code — "the entry
you expected to be there, is not"), rather than minting a new code for a case this
module's own orchestrator never legitimately triggers.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from vestibule.provisioning.model import (
    INDEX_PROVISION_CONFLICT,
    IndexRegistryEntry,
    ProvisioningError,
)


class IndexRegistry(ABC):
    """Persistence port every backend (`InMemoryIndexRegistry`,
    `TableStorageIndexRegistry`, `CachedIndexRegistry`) implements.

    Every mutating method is a single, backend-defined atomic operation — never a read
    call followed by a separate write call from the caller's side.
    """

    @abstractmethod
    def get(self, index_name: str) -> IndexRegistryEntry | None:
        """Returns the current entry, or `None` if unknown.

        Raises:
            ProvisioningError: `INDEX_REGISTRY_UNAVAILABLE` (TRANSIENT) only on a
                genuine backend failure (`TableStorageIndexRegistry` only, after
                exhausting retries). Never raises for a normal miss.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def list_by_vertical(self, vertical: str) -> list[IndexRegistryEntry]:
        """Returns every entry for `vertical`, order unspecified."""
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def register(self, entry: IndexRegistryEntry) -> IndexRegistryEntry:
        """Atomic conditional insert: insert-if-absent, else return the entry already
        stored, unconditionally.

        Never validates `entry`'s fields against what is already stored, and never
        raises purely because an entry already exists (the story's "the losing worker
        does not error").

        Returns:
            `entry` itself (by value) if this call performed the insert; the
            already-stored entry otherwise. Callers distinguish "did I win" via
            `returned.claim_token == entry.claim_token` — never via identity comparison
            (unsafe across a serialized backend) or wall-clock comparison (unsafe under
            clock skew).

        Raises:
            ProvisioningError: `INDEX_REGISTRY_UNAVAILABLE` (TRANSIENT) on a genuine
                backend failure (`TableStorageIndexRegistry` only). Never raises for a
                create-race.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def reclaim(
        self,
        index_name: str,
        observed: IndexRegistryEntry,
        new_entry: IndexRegistryEntry,
    ) -> IndexRegistryEntry:
        """The story's "second conditional write."

        Succeeds only if the entry currently stored for `index_name` is still exactly
        `observed` — replaces it with `new_entry` atomically. Never partially applies.

        Returns:
            `new_entry`, on success.

        Raises:
            ProvisioningError: `INDEX_PROVISION_CONFLICT` (TRANSIENT) if the stored
                entry has changed since `observed` was read (someone else reclaimed
                first, or the original claim owner finished normally) —
                `INDEX_REGISTRY_UNAVAILABLE` (TRANSIENT) on a genuine backend failure.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def touch_verified(self, index_name: str) -> IndexRegistryEntry:
        """Bumps `last_verified_at` to now on an already-`ready` entry.

        Not CAS-protected — no claim to protect; two concurrent touches are both
        harmless.

        Raises:
            ProvisioningError: `INDEX_REGISTRY_UNAVAILABLE` (TRANSIENT) on backend
                failure. Never raises for a status other than `ready`; callers only
                ever invoke this after already confirming `status == "ready"`.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """Flips `status -> "ready"`, stamps `last_verified_at = now`, clears
        `claim_token`/`claimed_at`/`resolved_template`.

        CAS-protected against `expected_claim_token` when non-`None`.

        Raises:
            ProvisioningError: `INDEX_PROVISION_CONFLICT` (TRANSIENT) if
                `expected_claim_token` is non-`None` and does not match the currently
                stored entry's `claim_token` (someone else already finalized this
                claim). `INDEX_REGISTRY_UNAVAILABLE` (TRANSIENT) on backend failure.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def mark_failed(
        self, index_name: str, reason: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """Flips `status -> "failed"`, records `last_error_message = reason`, clears
        `claim_token`/`claimed_at`/`resolved_template`.

        Same CAS semantics as `mark_ready`. Terminal: nothing in this module ever
        transitions a `failed` entry onward automatically.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def mark_retired(self, index_name: str) -> IndexRegistryEntry:
        """Flips `status -> "retired"`.

        Never called by `IndexProvisioner` itself — an explicit admin operation, no CAS
        needed since it is not racing an in-flight claim by construction.
        """
        raise NotImplementedError  # pragma: no cover


class InMemoryIndexRegistry(IndexRegistry):
    """Thread-safe, single-process `IndexRegistry` for tests and local dev.

    Backed by a `dict[str, IndexRegistryEntry]` guarded by one `threading.RLock` held
    for the full duration of each public method — the exact `InMemoryLedgerStore`
    pattern (REQ-003).

    Cross-process note (Contract #2 touchpoint): this backend proves nothing about
    cross-process safety — it IS one process. Its job is to correctly *simulate* the
    same claim/conflict/reclaim state machine `TableStorageIndexRegistry` enforces for
    real, deterministically, so real `threading`-based races can be exercised against
    it in tests.
    """

    def __init__(self) -> None:
        """Initializes an empty in-memory registry."""
        self._lock = threading.RLock()
        self._entries: dict[str, IndexRegistryEntry] = {}

    def get(self, index_name: str) -> IndexRegistryEntry | None:
        """See `IndexRegistry.get`."""
        with self._lock:
            return self._entries.get(index_name)

    def list_by_vertical(self, vertical: str) -> list[IndexRegistryEntry]:
        """See `IndexRegistry.list_by_vertical`."""
        with self._lock:
            return [e for e in self._entries.values() if e.vertical == vertical]

    def register(self, entry: IndexRegistryEntry) -> IndexRegistryEntry:
        """See `IndexRegistry.register`.

        "Atomic" means: the entire check-then-insert sequence executes inside one
        `with self._lock:` block — a single, uninterruptible critical section. This is
        not "safe because of the GIL": the GIL only guarantees a single bytecode
        operation is atomic, not a check-then-write sequence spanning two statements —
        without the lock, two threads could both observe absence before either writes.
        Whichever thread acquires the lock first executes its entire check-and-insert
        before the other's call can even begin its own check; the loser's check
        therefore always observes the winner's already-inserted entry.
        """
        with self._lock:
            existing = self._entries.get(entry.index_name)
            if existing is not None:
                return existing
            self._entries[entry.index_name] = entry
            return entry

    def reclaim(
        self,
        index_name: str,
        observed: IndexRegistryEntry,
        new_entry: IndexRegistryEntry,
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.reclaim`.

        "Atomic" means: inside one `with self._lock:` block, compare the currently
        stored entry to `observed` by full field equality (frozen pydantic
        `BaseModel.__eq__`, equivalent to an ETag comparison since every mutation on
        this backend also changes at least one field); if equal, replace with
        `new_entry` — all within the same lock acquisition, so no other call can
        interleave between the compare and the write.
        """
        with self._lock:
            current = self._entries.get(index_name)
            if current != observed:
                raise ProvisioningError(
                    index_name,
                    "reclaim observed a stale snapshot; the stored entry has since "
                    "changed",
                    error_code=INDEX_PROVISION_CONFLICT,
                )
            self._entries[index_name] = new_entry
            return new_entry

    def touch_verified(self, index_name: str) -> IndexRegistryEntry:
        """See `IndexRegistry.touch_verified`."""
        with self._lock:
            current = self._require(index_name)
            updated = current.model_copy(
                update={"last_verified_at": datetime.now(timezone.utc)}
            )
            self._entries[index_name] = updated
            return updated

    def mark_ready(
        self, index_name: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_ready`."""
        with self._lock:
            current = self._require(index_name)
            _check_claim_token(current, expected_claim_token)
            updated = current.model_copy(
                update={
                    "status": "ready",
                    "last_verified_at": datetime.now(timezone.utc),
                    "claim_token": None,
                    "claimed_at": None,
                    "resolved_template": None,
                }
            )
            self._entries[index_name] = updated
            return updated

    def mark_failed(
        self, index_name: str, reason: str, *, expected_claim_token: str | None
    ) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_failed`."""
        with self._lock:
            current = self._require(index_name)
            _check_claim_token(current, expected_claim_token)
            updated = current.model_copy(
                update={
                    "status": "failed",
                    "last_error_message": reason,
                    "claim_token": None,
                    "claimed_at": None,
                    "resolved_template": None,
                }
            )
            self._entries[index_name] = updated
            return updated

    def mark_retired(self, index_name: str) -> IndexRegistryEntry:
        """See `IndexRegistry.mark_retired`."""
        with self._lock:
            current = self._require(index_name)
            updated = current.model_copy(update={"status": "retired"})
            self._entries[index_name] = updated
            return updated

    def _require(self, index_name: str) -> IndexRegistryEntry:
        """Returns the current entry for `index_name`.

        Raises:
            ProvisioningError: `INDEX_PROVISION_CONFLICT` if no entry exists — see
                this module's own docstring for the spec-gap resolution.
        """
        current = self._entries.get(index_name)
        if current is None:
            raise ProvisioningError(
                index_name,
                "no registry entry exists for index_name",
                error_code=INDEX_PROVISION_CONFLICT,
            )
        return current


def _check_claim_token(
    current: IndexRegistryEntry, expected_claim_token: str | None
) -> None:
    """Raises `INDEX_PROVISION_CONFLICT` if `expected_claim_token` no longer matches.

    Args:
        current: The entry currently stored.
        expected_claim_token: The caller's own claim token, or `None` to skip the
            check (Assumption A5).

    Raises:
        ProvisioningError: `INDEX_PROVISION_CONFLICT` (TRANSIENT) if
            `expected_claim_token` is non-`None` and does not match
            `current.claim_token`.
    """
    if expected_claim_token is not None and current.claim_token != expected_claim_token:
        raise ProvisioningError(
            current.index_name,
            "claim_token no longer matches the stored entry (superseded by a "
            "reclaim or a concurrent finalization)",
            error_code=INDEX_PROVISION_CONFLICT,
        )
