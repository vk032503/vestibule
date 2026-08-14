"""`Scenario` cross-field validation checks (REQ-010).

Split out of `model.py` to keep that module's own model/converter definitions under the
house rules' module-size guidance (mirrors `vestibule.indexer.adapters
._azure_search_backend`'s precedent for the same reason). Every function here raises a
plain `ValueError` on failure — never this package's own `ScenarioError` — so that
`Scenario`'s `model_validator(mode="after")` (`model.py`) can let pydantic wrap it into a
`ValidationError`, exactly like `vestibule.envelope.model.ArrivalEnvelope`'s own
`_verify_doc_id` validator (REQ-001 precedent). Only `model.py` imports this module;
these functions are not part of this package's public API.

Spec-gap resolution (native embedding dimensions): see `model.py`'s own module
docstring for why `_NATIVE_DIMENSIONS` is a small, explicit, module-owned
`(adapter, model) -> native_dimensions` registry rather than sourced from a constructed
`EmbedderAdapter` instance.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vestibule.scenario.model import (
        ChunkerSettings,
        EmbedderSettings,
        IndexerSettings,
    )

_INDEX_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,126}[a-z0-9]$")
"""Azure AI Search index-naming rule (story): lowercase letters/digits/dashes, cannot
start or end with a dash. The two single-char anchors plus 0-126 middle characters bound
the total length at exactly [2, 128] characters, per the story's "2-128 chars"."""

_NATIVE_DIMENSIONS: dict[tuple[str, str], int] = {
    ("azure_openai", "text-embedding-3-large"): 3072,
    ("fastembed", "nomic-ai/nomic-embed-text-v1.5"): 768,
}
"""Static `(adapter, model) -> native output dimensions` registry — see `model.py`'s
module docstring for why this cannot be sourced from a constructed `EmbedderAdapter`
instance. Extend with a manual entry whenever a new (adapter, model) combination is
introduced; an unrecognized combination fails validation rather than silently skipping
the check."""


def validate_index_name(index_name: str) -> None:
    """Raises `ValueError` unless `index_name` matches `_INDEX_NAME_RE`."""
    if not _INDEX_NAME_RE.match(index_name):
        raise ValueError(
            f"index_name={index_name!r} is invalid: must be 2-128 characters, "
            "lowercase alphanumeric or dashes, and not start/end with a dash"
        )


def validate_chunker_bounds(chunker: "ChunkerSettings") -> None:
    """Raises `ValueError` unless `min <= target <= max` chunk-token bounds hold."""
    if not (
        chunker.min_chunk_tokens
        <= chunker.target_chunk_tokens
        <= chunker.max_chunk_tokens
    ):
        raise ValueError(
            "chunker token bounds violated: expected min_chunk_tokens "
            f"({chunker.min_chunk_tokens}) <= target_chunk_tokens "
            f"({chunker.target_chunk_tokens}) <= max_chunk_tokens "
            f"({chunker.max_chunk_tokens})"
        )


def validate_dimension_consistency(
    embedder: "EmbedderSettings", indexer: "IndexerSettings"
) -> None:
    """Raises `ValueError` on impossible truncation or an embedder/indexer dimension
    mismatch — the AC5 dimension-consistency guard.
    """
    key = (embedder.adapter, embedder.model)
    native = _NATIVE_DIMENSIONS.get(key)
    if native is None:
        raise ValueError(
            f"embedder (adapter={embedder.adapter!r}, model={embedder.model!r}) is not "
            "a recognized (adapter, model) combination in the native-dimensions "
            "registry; add an entry to _NATIVE_DIMENSIONS"
        )
    target = embedder.target_dimensions
    if target is not None and target > native:
        raise ValueError(
            f"embedder.target_dimensions={target} exceeds model {embedder.model!r}'s "
            f"native_dimensions={native}"
        )
    effective = target if target is not None else native
    if indexer.dimensions != effective:
        raise ValueError(
            f"indexer.dimensions={indexer.dimensions} does not match the embedder's "
            f"effective dimensions={effective} (target_dimensions={target!r}, "
            f"native={native})"
        )
