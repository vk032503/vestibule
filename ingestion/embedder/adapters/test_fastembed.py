"""Unit tests for FastEmbedEmbedder (REQ-007).

No real ONNX model is ever loaded — every capability/embedding test injects a fake
`_text_embedding` implementation via the adapter's internal test-injection seam. The
missing-dependency test relies on `fastembed` genuinely not being installed in this
project's test environment (it is the optional `local` extra, never a hard dependency),
so it needs no mocking at all — a fully hermetic, realistic exercise of the lazy-import
fail-fast path.
"""

from __future__ import annotations

import sys
import time
import types
from typing import Iterable

import pytest

from ingestion.embedder.adapters.fastembed import FastEmbedEmbedder
from ingestion.embedder.model import (
    EMBEDDER_DEPENDENCY_MISSING,
    EMBEDDER_TIMEOUT,
    EmbedderError,
)


class _FakeTextEmbedding:
    """A test-double standing in for `fastembed.TextEmbedding`."""

    def __init__(
        self,
        vectors: list[Iterable[float]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._vectors = vectors or []
        self._raise_exc = raise_exc
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> Iterable[Iterable[float]]:
        self.calls.append(list(texts))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._vectors


def _make_adapter(impl: _FakeTextEmbedding) -> FastEmbedEmbedder:
    return FastEmbedEmbedder(_text_embedding=impl)


# --- missing dependency (PERMANENT, fail fast) -------------------------------------------------


def test_fastembed_missing_dependency_raises_embedder_dependency_missing_permanent() -> (
    None
):
    with pytest.raises(EmbedderError) as exc_info:
        FastEmbedEmbedder()

    assert exc_info.value.error_code == EMBEDDER_DEPENDENCY_MISSING


# --- declared capabilities -----------------------------------------------------------------------


def test_default_capabilities() -> None:
    adapter = _make_adapter(_FakeTextEmbedding())
    assert adapter.model_name == "nomic-embed-text-v1.5"
    assert adapter.native_dimensions == 768
    assert adapter.max_batch_size == 32
    assert adapter.max_input_tokens == 8192
    assert adapter.supports_mrl is True
    assert adapter.document_prefix == "search_document: "


def test_custom_model_name_reflected_in_model_name_property() -> None:
    adapter = FastEmbedEmbedder("custom-model", _text_embedding=_FakeTextEmbedding())
    assert adapter.model_name == "custom-model"


# --- embed_batch -----------------------------------------------------------------------------------


def test_embed_batch_returns_vectors_in_order() -> None:
    impl = _FakeTextEmbedding(vectors=[[0.1, 0.2], [0.3, 0.4]])
    adapter = _make_adapter(impl)

    result = adapter.embed_batch(["a", "b"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert impl.calls == [["a", "b"]]


def test_embed_batch_converts_non_list_vector_likes_to_float_lists() -> None:
    """Guards against a real `fastembed.TextEmbedding.embed`, which yields numpy
    arrays rather than plain lists — `embed_batch` must generically float-convert
    whatever iterable-of-numbers each yielded vector is, not assume a Python list."""
    impl = _FakeTextEmbedding(vectors=[(1, 2, 3)])

    adapter = _make_adapter(impl)
    result = adapter.embed_batch(["a"])

    assert result == [[1.0, 2.0, 3.0]]
    assert all(isinstance(value, float) for value in result[0])


def test_embed_batch_unmapped_exception_propagates_unchanged() -> None:
    impl = _FakeTextEmbedding(raise_exc=RuntimeError("onnx runtime exploded"))
    adapter = _make_adapter(impl)

    with pytest.raises(RuntimeError, match="onnx runtime exploded"):
        adapter.embed_batch(["a"])


# --- model load timeout (bounds fastembed's network-dependent model download) --------------------


def test_model_load_exceeding_timeout_raises_embedder_timeout_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stands in a fake `fastembed` module whose `TextEmbedding` constructor never
    returns within the configured `load_timeout_seconds`, simulating a stalled
    model-weight download, and asserts it is bounded and mapped to `EMBEDDER_TIMEOUT`.
    """

    class _SlowTextEmbedding:
        def __init__(self, model_name: str, cache_dir: str | None) -> None:
            time.sleep(5.0)

    fake_module = types.ModuleType("fastembed")
    fake_module.TextEmbedding = _SlowTextEmbedding  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "fastembed", fake_module)

    with pytest.raises(EmbedderError) as exc_info:
        FastEmbedEmbedder(load_timeout_seconds=0.05)

    assert exc_info.value.error_code == EMBEDDER_TIMEOUT
