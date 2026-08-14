"""Scenario <-> Table Storage flat-entity conversion (REQ-010).

Split out of `table_storage_store.py` to keep that module's own orchestration/retry
logic under the house rules' module-size guidance (mirrors
`vestibule.indexer.adapters._azure_search_backend`'s precedent for the same reason).
Table Storage entities support only flat scalar properties, so nested settings
(`chunker`/`embedder`/`indexer`) round-trip as JSON strings.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from vestibule.scenario.model import (
    SCENARIO_INVALID,
    ChunkerSettings,
    EmbedderSettings,
    IndexerSettings,
    Scenario,
    ScenarioError,
)

if TYPE_CHECKING:
    from vestibule.scenario.stores.table_storage_store import _TableEntityRecord


def is_enabled(record: "_TableEntityRecord") -> bool:
    """Whether `record`'s stored `enabled` property is truthy."""
    return bool(record.data.get("enabled"))


def parse_datetime(value: Any) -> datetime:
    """Parses a stored `created_at`/`updated_at` ISO-8601 string back to `datetime`."""
    return datetime.fromisoformat(str(value))


def scenario_to_entity_data(scenario: Scenario) -> dict[str, Any]:
    """Flattens `scenario` into Table Storage's flat-property entity shape."""
    return {
        "scenario_id": scenario.scenario_id,
        "vertical": scenario.vertical,
        "config_version": scenario.config_version,
        "index_name": scenario.index_name,
        "acl_source": scenario.acl_source,
        "default_trust_tier": scenario.default_trust_tier,
        "enabled": scenario.enabled,
        "created_at": scenario.created_at.isoformat(),
        "updated_at": scenario.updated_at.isoformat(),
        "chunker_json": scenario.chunker.model_dump_json(),
        "embedder_json": scenario.embedder.model_dump_json(),
        "indexer_json": scenario.indexer.model_dump_json(),
    }


def entity_to_scenario(record: "_TableEntityRecord") -> Scenario:
    """Reconstructs a `Scenario` from a stored `_TableEntityRecord`.

    Raises:
        ScenarioError: `SCENARIO_INVALID` (PERMANENT) if the stored entity is corrupt
            (missing/malformed properties) or fails `Scenario`'s own validation.
    """
    data = record.data
    try:
        return Scenario(
            scenario_id=data["scenario_id"],
            vertical=data["vertical"],
            config_version=data["config_version"],
            index_name=data["index_name"],
            chunker=ChunkerSettings.model_validate_json(data["chunker_json"]),
            embedder=EmbedderSettings.model_validate_json(data["embedder_json"]),
            indexer=IndexerSettings.model_validate_json(data["indexer_json"]),
            acl_source=data["acl_source"],
            default_trust_tier=data["default_trust_tier"],
            enabled=bool(data["enabled"]),
            created_at=parse_datetime(data["created_at"]),
            updated_at=parse_datetime(data["updated_at"]),
        )
    except (ValidationError, KeyError, ValueError) as exc:
        raise ScenarioError(
            str(data.get("scenario_id", "")),
            f"corrupt table entity: {exc}",
            error_code=SCENARIO_INVALID,
        ) from exc
