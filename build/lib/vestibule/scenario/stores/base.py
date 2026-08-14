"""ScenarioStore — abstract lookup/config port for per-vertical ingestion settings
(REQ-010).

`ScenarioStore` is a pure lookup/config component, read at runtime by whichever backend
is configured (`config/scenario_store.yaml`'s `backend` key): `YamlScenarioStore`
(read-only, version-controlled) or `TableStorageScenarioStore` (writable,
runtime-editable). `CachedScenarioStore` decorates either backend with a TTL cache.

Contract #3 touchpoint: this module writes no `LedgerRow`, ever — a `ScenarioStore` is a
config lookup, not a pipeline stage (story: "no transitions owned by this component").
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from vestibule.scenario.model import SCENARIO_NOT_FOUND, Scenario, ScenarioError

logger = logging.getLogger(__name__)


class ScenarioStore(ABC):
    """Lookup/config port every backend (`YamlScenarioStore`,
    `TableStorageScenarioStore`, `CachedScenarioStore`) implements."""

    @abstractmethod
    def get(self, scenario_id: str) -> Scenario | None:
        """Looks up one scenario by its unique `scenario_id`.

        Args:
            scenario_id: The scenario to look up.

        Returns:
            The matching `Scenario`, or `None` if no scenario with that id exists.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def get_by_vertical(self, vertical: str) -> Scenario | None:
        """Looks up the enabled scenario for `vertical`.

        Args:
            vertical: The vertical to look up, e.g. `"hr"`.

        Returns:
            The single `enabled=True` `Scenario` for `vertical`, or `None` if none
            exists. If more than one enabled scenario exists for the same `vertical`,
            see `select_enabled_for_vertical` for the deterministic tie-break.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def list_all(self, include_disabled: bool = False) -> list[Scenario]:
        """Lists every stored scenario.

        Args:
            include_disabled: When `False` (default), scenarios with `enabled=False`
                are excluded.

        Returns:
            Every matching `Scenario`, order unspecified.
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def upsert(self, scenario: Scenario) -> Scenario:
        """Creates or updates `scenario`, bumping `config_version`/`updated_at`.

        Args:
            scenario: The scenario to write. Re-validated before the write lands (so a
                bad write can never land), even though `Scenario`'s own construction
                already validated it once (defense in depth).

        Returns:
            The stored `Scenario`, with `config_version`/`updated_at` reflecting this
            write (not necessarily identical to the input `scenario`).

        Raises:
            ScenarioError: `SCENARIO_STORE_READ_ONLY` (PERMANENT) if `is_writable` is
                `False`; `SCENARIO_INVALID` (PERMANENT) if re-validation fails;
                `SCENARIO_CONFLICT` (TRANSIENT) on an optimistic-concurrency conflict
                (`TableStorageScenarioStore` only).
        """
        raise NotImplementedError  # pragma: no cover

    @abstractmethod
    def delete(self, scenario_id: str) -> bool:
        """Deletes the scenario keyed by `scenario_id`, if any.

        Args:
            scenario_id: The scenario to delete.

        Returns:
            `True` if a scenario was deleted; `False` if none existed.

        Raises:
            ScenarioError: `SCENARIO_STORE_READ_ONLY` (PERMANENT) if `is_writable` is
                `False`.
        """
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def is_writable(self) -> bool:
        """Whether `upsert`/`delete` are supported by this backend."""
        raise NotImplementedError  # pragma: no cover

    def resolve(
        self,
        *,
        scenario_id: str | None = None,
        vertical: str | None = None,
        default_scenario_id: str | None = None,
    ) -> Scenario:
        """Resolves a `Scenario` from a document's envelope fields, with a fallback.

        Spec-gap resolution: routing a document to a `vertical`/`scenario_id` in the
        first place is REQ-009's Vertical Loader (out of scope here), but
        `default_scenario_id` is this store's own config surface
        (`config/scenario_store.yaml`), so the fallback lookup it enables belongs here —
        implemented once via the five methods above rather than duplicated per backend.

        Args:
            scenario_id: The arrival envelope's `scenario_id`, if set — tried first.
            vertical: The arrival envelope's `vertical`, if set and `scenario_id` was
                not, or did not resolve — tried second.
            default_scenario_id: `config/scenario_store.yaml`'s configured fallback —
                tried last.

        Returns:
            The first resolved `Scenario`, in the order above.

        Raises:
            ScenarioError: `SCENARIO_NOT_FOUND` (PERMANENT) if none of `scenario_id`,
                `vertical`, or `default_scenario_id` resolves to a `Scenario`.
        """
        if scenario_id is not None:
            found = self.get(scenario_id)
            if found is not None:
                return found
        if vertical is not None:
            found = self.get_by_vertical(vertical)
            if found is not None:
                return found
        if default_scenario_id is not None:
            found = self.get(default_scenario_id)
            if found is not None:
                logger.warning(
                    "scenario_store_resolved_via_default_scenario_id",
                    extra={
                        "default_scenario_id": default_scenario_id,
                        "vertical": vertical,
                    },
                )
                return found
        raise ScenarioError(
            scenario_id or "",
            f"no scenario found for scenario_id={scenario_id!r} "
            f"vertical={vertical!r}, and no default_scenario_id resolved",
            error_code=SCENARIO_NOT_FOUND,
        )


def select_enabled_for_vertical(
    vertical: str, candidates: list[Scenario]
) -> Scenario | None:
    """Picks one `enabled=True` scenario for `vertical`, deterministically.

    Spec-gap resolution (story: "if multiple enabled scenarios somehow exist for one
    vertical, that's a legitimate ambiguity to document/handle explicitly"): this REQ
    does not prevent two enabled scenarios sharing a `vertical` (`upsert` validates one
    `Scenario` in isolation, never scans siblings) — resolved here by picking the
    lexicographically smallest `scenario_id`, deterministic across repeated calls and
    independent of backend iteration order, and logging a warning so the ambiguity is
    observable rather than silent.

    Args:
        vertical: The vertical being looked up, for the warning log only.
        candidates: Every `enabled=True` scenario already filtered to `vertical`.

    Returns:
        The candidate with the lexicographically smallest `scenario_id`, or `None` if
        `candidates` is empty.
    """
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "scenario_store_multiple_enabled_scenarios_for_vertical",
            extra={
                "vertical": vertical,
                "scenario_ids": sorted(c.scenario_id for c in candidates),
            },
        )
    return min(candidates, key=lambda c: c.scenario_id)
