"""IndexProvisioner — the single public entry point (REQ-011).

Orchestrates template resolution, the atomic-claim/reclaim state machine, drift
verification, and index creation. Owns no I/O algorithm itself — every I/O call is
delegated to `IndexRegistry`/`IndexProvisionerAdapter` (house rules: "adapters thin",
"never implement ... in-house"). See `docs/designs/REQ-011-lld.md` §3 for the full
numbered sequence walkthrough this module implements.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

from vestibule.provisioning.adapters.base import IndexProvisionerAdapter
from vestibule.provisioning.model import (
    INDEX_AUTO_CREATE_DISABLED,
    INDEX_PROVISION_CONFLICT,
    INDEX_PROVISION_FAILED,
    INDEX_PROVISION_TIMEOUT,
    INDEX_PROVISIONER_UNAVAILABLE,
    INDEX_RETIRED,
    INDEX_SCHEMA_DRIFT,
    INDEX_TEMPLATE_INVALID,
    IndexRegistryEntry,
    IndexTemplate,
    ProvisioningError,
)
from vestibule.provisioning.registry import IndexRegistry
from vestibule.provisioning.templates import IndexTemplateStore
from vestibule.scenario.model import Scenario


@dataclass(frozen=True)
class ProvisioningConfig:
    """Documentation-authoritative-but-code-defaults tunables (REQ-006 §6 precedent —
    no config-loading mechanism for *tunables* exists in this codebase yet;
    `config/index_provisioning.yaml` is kept in sync via a dedicated test).

    Attributes:
        default_template_id: Fallback template id used when
            `scenario.index_template_id` is `None`.
        provisioning_stale_after_seconds: How long a `provisioning` claim may sit
            untouched before a waiting worker may reclaim it.
        verification_interval_seconds: How long a `ready` entry's drift verification
            result is trusted before re-checking the live index.
        wait_for_provisioning_timeout_seconds: The overall bound a waiting worker
            tolerates before raising `INDEX_PROVISION_TIMEOUT`.
        wait_poll_interval_seconds: Fixed polling cadence within
            `wait_for_provisioning_timeout_seconds` (Assumption A8).
        auto_create: When `False`, a missing index raises
            `INDEX_AUTO_CREATE_DISABLED` rather than provisioning.
    """

    default_template_id: str = "standard-v1"
    provisioning_stale_after_seconds: float = 300.0
    verification_interval_seconds: float = 300.0
    wait_for_provisioning_timeout_seconds: float = 60.0
    wait_poll_interval_seconds: float = 2.0
    auto_create: bool = True


class IndexProvisioner:
    """Orchestrates the full REQ-011 provisioning flow. See this module's docstring."""

    def __init__(
        self,
        registry: IndexRegistry,
        adapter: IndexProvisionerAdapter,
        templates: IndexTemplateStore,
        config: ProvisioningConfig,
        *,
        _clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        _sleep: Callable[[float], None] = time.sleep,
        _new_claim_token: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        """Initializes the provisioner.

        Args:
            registry: The `IndexRegistry` backend — may be a bare
                `TableStorageIndexRegistry`/`InMemoryIndexRegistry` or a
                `CachedIndexRegistry`-wrapped one; this constructor's signature is
                unchanged either way.
            adapter: The `IndexProvisionerAdapter` every index-creation/drift call
                dispatches to.
            templates: The `IndexTemplateStore` every template resolution reads from.
            config: Operational tunables.
            _clock: Internal test-injection seam for the wall clock.
            _sleep: Internal test-injection seam for the wait loop's sleep function.
            _new_claim_token: Internal test-injection seam for claim-token generation.
        """
        self._registry = registry
        self._adapter = adapter
        self._templates = templates
        self._config = config
        self._clock = _clock
        self._sleep = _sleep
        self._new_claim_token = _new_claim_token

    def ensure(self, scenario: Scenario) -> IndexRegistryEntry:
        """The single public entry point.

        Reads `scenario.index_name`/`.vertical`/`.scenario_id`/`.index_template_id`
        (Assumption A1)/`.embedder.model`/`.indexer.dimensions` (Assumption A2) — no
        `ArrivalEnvelope` field (this module's Contract #1 touchpoint: "not read
        directly").

        Args:
            scenario: The scenario whose index must exist and be compatible.

        Returns:
            The current, ready `IndexRegistryEntry` for `scenario.index_name`.

        Raises:
            ProvisioningError: See `docs/designs/REQ-011-lld.md` §5 for the full
                trigger-condition table across all eleven error codes.
        """
        entry = self._registry.get(scenario.index_name)
        if entry is None:
            return self._claim_new(scenario)
        return self._dispatch_existing(scenario, entry)

    # --- dispatch -----------------------------------------------------------------

    def _dispatch_existing(
        self, scenario: Scenario, entry: IndexRegistryEntry
    ) -> IndexRegistryEntry:
        """Dispatches on an entry observed directly via `registry.get()`."""
        if entry.status == "ready":
            return self._handle_ready(scenario, entry)
        if entry.status == "provisioning":
            return self._wait_for_ready(scenario, entry)
        if entry.status == "failed":
            raise _failed_error(entry)
        raise _retired_error(entry)

    def _after_losing_claim(
        self, scenario: Scenario, entry: IndexRegistryEntry
    ) -> IndexRegistryEntry:
        """Dispatches on an entry observed after losing a `register`/`reclaim` race.

        Differs from `_dispatch_existing` only for `status == "ready"`: this entry was
        just authoritatively resolved by a registry write a moment ago, so it is
        returned directly without re-running verification.
        """
        if entry.status == "ready":
            return entry
        if entry.status == "provisioning":
            return self._wait_for_ready(scenario, entry)
        if entry.status == "failed":
            raise _failed_error(entry)
        raise _retired_error(entry)

    # --- ready / verification-elapsed ----------------------------------------------

    def _handle_ready(
        self, scenario: Scenario, entry: IndexRegistryEntry
    ) -> IndexRegistryEntry:
        """The fast path (§3.1) plus the verification-elapsed sub-case (§3.2)."""
        if entry.last_verified_at is not None and not _elapsed(
            self._clock(),
            entry.last_verified_at,
            self._config.verification_interval_seconds,
        ):
            return entry
        template, resolved_dimensions, resolved_metric = self._resolve_template(
            scenario
        )
        description = self._call_adapter(self._adapter.describe_index, entry.index_name)
        drifted = (
            description is None
            or description.dimensions != resolved_dimensions
            or description.metric != resolved_metric
            or int(entry.template_version) < int(template.template_version)
        )
        if drifted:
            raise ProvisioningError(
                entry.index_name,
                f"live index incompatible with resolved template: description="
                f"{description!r}, resolved_dimensions={resolved_dimensions}, "
                f"resolved_metric={resolved_metric!r}, entry.template_version="
                f"{entry.template_version!r}, template.template_version="
                f"{template.template_version!r}",
                error_code=INDEX_SCHEMA_DRIFT,
            )
        return self._registry.touch_verified(entry.index_name)

    # --- no-entry / claim -----------------------------------------------------------

    def _claim_new(self, scenario: Scenario) -> IndexRegistryEntry:
        """The no-entry sub-case (§3.2): claim, then create."""
        if not self._config.auto_create:
            raise ProvisioningError(
                scenario.index_name,
                "no index exists and config.auto_create is False",
                error_code=INDEX_AUTO_CREATE_DISABLED,
            )
        template, resolved_dimensions, resolved_metric = self._resolve_template(
            scenario
        )
        now = self._clock()
        candidate = IndexRegistryEntry(
            index_name=scenario.index_name,
            vertical=scenario.vertical,
            scenario_id=scenario.scenario_id,
            template_id=template.template_id,
            template_version=template.template_version,
            dimensions=resolved_dimensions,
            metric=resolved_metric,
            embedding_model=scenario.embedder.model,
            status="provisioning",
            created_at=now,
            last_verified_at=None,
            document_count=None,
            claim_token=self._new_claim_token(),
            claimed_at=now,
            last_error_message=None,
            resolved_template=template,
        )
        won_entry = self._registry.register(candidate)
        if won_entry.claim_token == candidate.claim_token:
            return self._claim_and_create(scenario, won_entry)
        return self._after_losing_claim(scenario, won_entry)

    def _resolve_template(self, scenario: Scenario) -> tuple[IndexTemplate, int, str]:
        """Resolves the template/dimensions/metric a claim or verification uses.

        Returns:
            `(template, resolved_dimensions, resolved_metric)`.

        Raises:
            ProvisioningError: `INDEX_TEMPLATE_NOT_FOUND` if the resolved
                `template_id` is unknown; `INDEX_TEMPLATE_INVALID` (Assumption A2) if
                the template's explicit `dimensions` disagrees with
                `scenario.indexer.dimensions`.
        """
        template_id = scenario.index_template_id or self._config.default_template_id
        template = self._templates.get_or_raise(template_id)
        if (
            template.dimensions is not None
            and template.dimensions != scenario.indexer.dimensions
        ):
            raise ProvisioningError(
                scenario.index_name,
                f"template {template.template_id!r}'s explicit dimensions="
                f"{template.dimensions} disagrees with scenario.indexer.dimensions="
                f"{scenario.indexer.dimensions}",
                error_code=INDEX_TEMPLATE_INVALID,
            )
        resolved_dimensions = (
            template.dimensions
            if template.dimensions is not None
            else scenario.indexer.dimensions
        )
        return template, resolved_dimensions, template.metric

    def _claim_and_create(
        self, scenario: Scenario, won_entry: IndexRegistryEntry
    ) -> IndexRegistryEntry:
        """The shared "I hold the claim, make it real" sub-flow (§3.2 steps 5-10).

        Reads the template/dimensions/metric it creates the index with directly off
        `won_entry.resolved_template`/`.dimensions`/`.metric` (Assumption A14) — used
        by both the original winner and a successful reclaimer.
        """
        index_name = won_entry.index_name
        already_exists = self._call_adapter(self._adapter.index_exists, index_name)
        if not already_exists:
            assert won_entry.resolved_template is not None
            try:
                self._call_adapter(
                    self._adapter.create_index,
                    index_name,
                    won_entry.resolved_template,
                    won_entry.dimensions,
                    won_entry.metric,
                )
            except ProvisioningError as exc:
                self._absorb_conflict(
                    self._registry.mark_failed,
                    index_name,
                    str(exc),
                    expected_claim_token=won_entry.claim_token,
                )
                raise
        description = self._call_adapter(self._adapter.describe_index, index_name)
        if description is None or (
            description.dimensions,
            description.metric,
        ) != (won_entry.dimensions, won_entry.metric):
            reason = (
                f"index {index_name!r} incompatible after creation/existence check: "
                f"description={description!r}, expected dimensions="
                f"{won_entry.dimensions}, metric={won_entry.metric!r}"
            )
            self._absorb_conflict(
                self._registry.mark_failed,
                index_name,
                reason,
                expected_claim_token=won_entry.claim_token,
            )
            raise ProvisioningError(index_name, reason, error_code=INDEX_SCHEMA_DRIFT)
        return self._finalize_ready(scenario, won_entry)

    def _finalize_ready(
        self, scenario: Scenario, won_entry: IndexRegistryEntry
    ) -> IndexRegistryEntry:
        """Marks `won_entry` ready, absorbing a lost finalization race (§3.2 step 9)."""
        try:
            return self._registry.mark_ready(
                won_entry.index_name, expected_claim_token=won_entry.claim_token
            )
        except ProvisioningError as exc:
            if exc.error_code != INDEX_PROVISION_CONFLICT:
                raise
            current = self._registry.get(won_entry.index_name)
            if current is None:
                return self._claim_new(scenario)
            return self._after_losing_claim(scenario, current)

    def _absorb_conflict(
        self, fn: Callable[..., IndexRegistryEntry], *args: Any, **kwargs: Any
    ) -> None:
        """Calls a finalization write, swallowing only `INDEX_PROVISION_CONFLICT`.

        A reclaimer may have already finalized this claim by the time this call runs
        (Race B, sub-ordering 4b) — that is expected and harmless, not an error this
        call site needs to react to.
        """
        try:
            fn(*args, **kwargs)
        except ProvisioningError as exc:
            if exc.error_code != INDEX_PROVISION_CONFLICT:
                raise

    # --- wait / reclaim --------------------------------------------------------------

    def _wait_for_ready(
        self, scenario: Scenario, entry: IndexRegistryEntry
    ) -> IndexRegistryEntry:
        """The bounded poll/reclaim loop (§3.4)."""
        current = entry
        deadline = self._clock() + timedelta(
            seconds=self._config.wait_for_provisioning_timeout_seconds
        )
        while True:
            if self._is_stale(current):
                reclaimed = self._try_reclaim(current)
                if reclaimed is not None:
                    return self._claim_and_create(scenario, reclaimed)
            if self._clock() >= deadline:
                raise ProvisioningError(
                    entry.index_name,
                    "timed out waiting for another worker's provisioning claim to "
                    "reach ready",
                    error_code=INDEX_PROVISION_TIMEOUT,
                )
            self._sleep(self._config.wait_poll_interval_seconds)
            refreshed = self._registry.get(entry.index_name)
            if refreshed is None:
                return self._claim_new(scenario)
            if refreshed.status != "provisioning":
                return self._after_losing_claim(scenario, refreshed)
            current = refreshed

    def _is_stale(self, entry: IndexRegistryEntry) -> bool:
        """Whether `entry`'s claim has outlived `provisioning_stale_after_seconds`."""
        if entry.claimed_at is None:
            return False
        return _elapsed(
            self._clock(),
            entry.claimed_at,
            self._config.provisioning_stale_after_seconds,
        )

    def _try_reclaim(self, observed: IndexRegistryEntry) -> IndexRegistryEntry | None:
        """Attempts to reclaim a stale claim (§3.4 step 1-2).

        `new_candidate` copies every resolved field verbatim from `observed` via
        `model_copy` (Assumption A14) — only `claim_token`/`claimed_at` change;
        `created_at`/`template_id`/`template_version`/`dimensions`/`metric`/
        `resolved_template`/`embedding_model`/`vertical`/`scenario_id` are preserved
        exactly.

        Returns:
            The new, reclaimed entry on success; `None` if the reclaim lost the race
            (Race A/B, absorbed — the caller falls back to the ordinary poll loop).
        """
        new_candidate = observed.model_copy(
            update={
                "claim_token": self._new_claim_token(),
                "claimed_at": self._clock(),
            }
        )
        try:
            return self._registry.reclaim(
                observed.index_name, observed=observed, new_entry=new_candidate
            )
        except ProvisioningError as exc:
            if exc.error_code == INDEX_PROVISION_CONFLICT:
                return None
            raise

    # --- adapter call wrapping (Assumption A7) ---------------------------------------

    def _call_adapter(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Calls an `IndexProvisionerAdapter` method, wrapping any unmapped exception.

        Raises:
            ProvisioningError: Propagated unchanged if already a `ProvisioningError`
                (already classified by the adapter itself); otherwise wrapped as
                `INDEX_PROVISIONER_UNAVAILABLE` (TRANSIENT) — the closest declared
                code (Assumption A7).
        """
        index_name = str(args[0]) if args else ""
        try:
            return fn(*args)
        except ProvisioningError:
            raise
        except Exception as exc:  # noqa: BLE001 -- immediately re-raised, classified.
            raise ProvisioningError(
                index_name, str(exc), error_code=INDEX_PROVISIONER_UNAVAILABLE
            ) from exc


def _elapsed(now: datetime, since: datetime, threshold_seconds: float) -> bool:
    """Whether `now - since >= threshold_seconds`."""
    return (now - since) >= timedelta(seconds=threshold_seconds)


def _failed_error(entry: IndexRegistryEntry) -> ProvisioningError:
    """Builds the `INDEX_PROVISION_FAILED` error for a terminally-failed entry."""
    return ProvisioningError(
        entry.index_name,
        entry.last_error_message
        or "index provisioning failed; requires human intervention",
        error_code=INDEX_PROVISION_FAILED,
    )


def _retired_error(entry: IndexRegistryEntry) -> ProvisioningError:
    """Builds the `INDEX_RETIRED` error for a retired entry."""
    return ProvisioningError(
        entry.index_name, "index has been retired", error_code=INDEX_RETIRED
    )
