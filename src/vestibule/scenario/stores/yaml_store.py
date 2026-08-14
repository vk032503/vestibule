"""YamlScenarioStore — read-only, file-backed ScenarioStore (REQ-010).

Reads one `Scenario` per `*.yaml` file in a directory, at construction and on explicit
`reload()` only — never automatically (story: "Reloads on explicit reload() call, not
automatically"). Every scenario is validated by `Scenario`'s own constructor
(`vestibule.scenario.model`); a validation failure is caught here and re-raised as this
package's own `ScenarioError(SCENARIO_INVALID)` (mirrors
`vestibule.envelope.model._wrap_validation_error`) — so a malformed
`config/scenarios/*.yaml` file fails the whole store's construction, before any document
is ever in scope (AC5).

Read-only by design (story: "simple, version-controlled, good for tests and
single-tenant deployments") — `upsert`/`delete` always raise
`SCENARIO_STORE_READ_ONLY` (PERMANENT).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from vestibule.scenario.model import (
    SCENARIO_INVALID,
    SCENARIO_STORE_READ_ONLY,
    Scenario,
    ScenarioError,
)
from vestibule.scenario.stores.base import ScenarioStore, select_enabled_for_vertical


class YamlScenarioStore(ScenarioStore):
    """Read-only `ScenarioStore` backed by `config/scenarios/*.yaml`-shaped files."""

    def __init__(self, directory: str | Path) -> None:
        """Loads every scenario file under `directory`.

        Args:
            directory: Directory containing one `*.yaml` file per scenario.

        Raises:
            ScenarioError: `SCENARIO_INVALID` (PERMANENT) if any file fails to parse as
                YAML or fails `Scenario` validation — fail fast (AC5), before any
                document is ever in scope.
        """
        self._directory = Path(directory)
        self._scenarios: dict[str, Scenario] = {}
        self.reload()

    @property
    def is_writable(self) -> bool:
        """See `ScenarioStore.is_writable`. Always `False`."""
        return False

    def reload(self) -> None:
        """Re-reads every `*.yaml` file under `self._directory` from disk.

        Raises:
            ScenarioError: `SCENARIO_INVALID` (PERMANENT), same as `__init__`.
        """
        loaded: dict[str, Scenario] = {}
        for path in sorted(self._directory.glob("*.yaml")):
            scenario = _load_one(path)
            loaded[scenario.scenario_id] = scenario
        self._scenarios = loaded

    def get(self, scenario_id: str) -> Scenario | None:
        """See `ScenarioStore.get`."""
        return self._scenarios.get(scenario_id)

    def get_by_vertical(self, vertical: str) -> Scenario | None:
        """See `ScenarioStore.get_by_vertical`."""
        candidates = [
            s for s in self._scenarios.values() if s.vertical == vertical and s.enabled
        ]
        return select_enabled_for_vertical(vertical, candidates)

    def list_all(self, include_disabled: bool = False) -> list[Scenario]:
        """See `ScenarioStore.list_all`."""
        values = self._scenarios.values()
        if include_disabled:
            return list(values)
        return [s for s in values if s.enabled]

    def upsert(self, scenario: Scenario) -> Scenario:
        """See `ScenarioStore.upsert`.

        Raises:
            ScenarioError: `SCENARIO_STORE_READ_ONLY` (PERMANENT), always.
        """
        raise ScenarioError(
            scenario.scenario_id,
            "YamlScenarioStore is read-only; edit the underlying YAML file and call "
            "reload() instead",
            error_code=SCENARIO_STORE_READ_ONLY,
        )

    def delete(self, scenario_id: str) -> bool:
        """See `ScenarioStore.delete`.

        Raises:
            ScenarioError: `SCENARIO_STORE_READ_ONLY` (PERMANENT), always.
        """
        raise ScenarioError(
            scenario_id,
            "YamlScenarioStore is read-only; edit the underlying YAML file and call "
            "reload() instead",
            error_code=SCENARIO_STORE_READ_ONLY,
        )


def _load_one(path: Path) -> Scenario:
    """Parses and validates one scenario YAML file.

    Args:
        path: The `*.yaml` file to load.

    Returns:
        The validated `Scenario`.

    Raises:
        ScenarioError: `SCENARIO_INVALID` (PERMANENT) on a YAML parse error, a
            non-mapping document, or a `Scenario` validation failure.
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(
            "", f"{path}: invalid YAML: {exc}", error_code=SCENARIO_INVALID
        ) from exc
    if not isinstance(raw, dict):
        raise ScenarioError(
            "",
            f"{path}: expected a YAML mapping at the document root, got "
            f"{type(raw).__name__}",
            error_code=SCENARIO_INVALID,
        )
    try:
        return Scenario(**raw)
    except ValidationError as exc:
        scenario_id = raw.get("scenario_id", "")
        raise ScenarioError(
            scenario_id, f"{path}: {exc}", error_code=SCENARIO_INVALID
        ) from exc
