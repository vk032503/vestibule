"""IndexRegistryEntry <-> Table Storage flat-entity conversion (REQ-011).

Split out of `table_storage_registry.py` to keep that module's own orchestration/retry
logic under the house rules' module-size guidance (mirrors
`vestibule.scenario.stores._entity_codec`'s precedent for the same reason). Table
Storage entities support only flat scalar properties, so `resolved_template` (a nested
`IndexTemplate`) round-trips as a JSON string, the same approach
`vestibule.scenario.stores._entity_codec` uses for `chunker`/`embedder`/`indexer`.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from vestibule.provisioning.model import (
    INDEX_REGISTRY_UNAVAILABLE,
    IndexRegistryEntry,
    IndexTemplate,
    ProvisioningError,
)

if TYPE_CHECKING:
    from vestibule.provisioning.stores.table_storage_registry import (
        _TableEntityRecord,
    )


def entry_to_entity_data(entry: IndexRegistryEntry) -> dict[str, Any]:
    """Flattens `entry` into Table Storage's flat-property entity shape.

    Optional (`None`-able) fields are omitted from the returned mapping rather than
    written as an explicit null — `_RealTableBackend.upsert_entity` always issues a
    full-replace write (`UpdateMode.REPLACE`/an unconditional create), so an omitted
    key never leaves a stale prior value behind.
    """
    data: dict[str, Any] = {
        "index_name": entry.index_name,
        "vertical": entry.vertical,
        "scenario_id": entry.scenario_id,
        "template_id": entry.template_id,
        "template_version": entry.template_version,
        "dimensions": entry.dimensions,
        "metric": entry.metric,
        "embedding_model": entry.embedding_model,
        "status": entry.status,
        "created_at": entry.created_at.isoformat(),
    }
    _set_optional(
        data,
        "last_verified_at",
        entry.last_verified_at.isoformat() if entry.last_verified_at else None,
    )
    _set_optional(data, "document_count", entry.document_count)
    _set_optional(data, "claim_token", entry.claim_token)
    _set_optional(
        data, "claimed_at", entry.claimed_at.isoformat() if entry.claimed_at else None
    )
    _set_optional(data, "last_error_message", entry.last_error_message)
    _set_optional(
        data,
        "resolved_template_json",
        entry.resolved_template.model_dump_json() if entry.resolved_template else None,
    )
    return data


def entity_to_entry(record: "_TableEntityRecord") -> IndexRegistryEntry:
    """Reconstructs an `IndexRegistryEntry` from a stored `_TableEntityRecord`.

    Spec-gap resolution (error code for a corrupt stored entity): unlike
    `vestibule.scenario.stores._entity_codec`, this module's 11 error codes include no
    direct analog of `SCENARIO_INVALID` for "the store returned data this module
    cannot deserialize." `INDEX_REGISTRY_UNAVAILABLE` (TRANSIENT) is used instead — the
    closest declared code (the registry failed to return a usable answer), consistent
    with Assumption A7's "closest declared code" resolution for the adapter side and
    Contract #4's "unclassified errors default to TRANSIENT."

    Raises:
        ProvisioningError: `INDEX_REGISTRY_UNAVAILABLE` if the stored entity is corrupt
            (missing/malformed properties) or fails `IndexRegistryEntry` validation.
    """
    data = record.data
    try:
        return IndexRegistryEntry(
            index_name=data["index_name"],
            vertical=data["vertical"],
            scenario_id=data["scenario_id"],
            template_id=data["template_id"],
            template_version=data["template_version"],
            dimensions=int(data["dimensions"]),
            metric=data["metric"],
            embedding_model=data["embedding_model"],
            status=data["status"],
            created_at=_parse_datetime(data["created_at"]),
            last_verified_at=_parse_optional_datetime(data.get("last_verified_at")),
            document_count=_parse_optional_int(data.get("document_count")),
            claim_token=data.get("claim_token"),
            claimed_at=_parse_optional_datetime(data.get("claimed_at")),
            last_error_message=data.get("last_error_message"),
            resolved_template=_parse_optional_template(
                data.get("resolved_template_json")
            ),
        )
    except (ValidationError, KeyError, ValueError) as exc:
        raise ProvisioningError(
            str(data.get("index_name", "")),
            f"corrupt table entity: {exc}",
            error_code=INDEX_REGISTRY_UNAVAILABLE,
        ) from exc


def _set_optional(data: dict[str, Any], key: str, value: Any) -> None:
    """Sets `data[key] = value` only when `value is not None`."""
    if value is not None:
        data[key] = value


def _parse_datetime(value: Any) -> datetime:
    """Parses a stored ISO-8601 datetime string."""
    return datetime.fromisoformat(str(value))


def _parse_optional_datetime(value: Any) -> datetime | None:
    """Parses a stored, possibly-absent ISO-8601 datetime string."""
    return _parse_datetime(value) if value is not None else None


def _parse_optional_int(value: Any) -> int | None:
    """Parses a stored, possibly-absent integer property."""
    return int(value) if value is not None else None


def _parse_optional_template(value: Any) -> IndexTemplate | None:
    """Parses a stored, possibly-absent JSON-serialized `IndexTemplate`."""
    return IndexTemplate.model_validate_json(str(value)) if value is not None else None
