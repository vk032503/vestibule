"""FastEmbedEmbedder — thin EmbedderAdapter wrapping `fastembed` (REQ-007).

Local, credentials-free path: ONNX-based, CPU-only, no PyTorch. Default model
`nomic-embed-text-v1.5` (768 native dimensions, Matryoshka-capable, ~274MB). `fastembed`
is an optional dependency (the `local` extra) — imported lazily inside `__init__` so
importing this module never hard-fails for callers who did not install the extra; a
missing dependency raises `EMBEDDER_DEPENDENCY_MISSING` (PERMANENT) at construction
time, not at embed time (same fail-fast shape as `EMBEDDER_MRL_UNSUPPORTED`).

On first use, `fastembed.TextEmbedding`'s constructor downloads ONNX model weights over
the network (HuggingFace Hub under the hood) — an external call, so it is bounded by a
timeout (house rule: "every external call: timeout + declared error classification"),
via a `ThreadPoolExecutor`/`Future` (no new dependency); an expired timeout raises
`EMBEDDER_TIMEOUT` (TRANSIENT).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Iterable, Protocol

from ingestion.embedder.adapters.base import EmbedderAdapter
from ingestion.embedder.model import (
    EMBEDDER_DEPENDENCY_MISSING,
    EMBEDDER_TIMEOUT,
    EmbedderError,
)

_DEFAULT_MODEL = "nomic-embed-text-v1.5"
_NATIVE_DIMENSIONS = 768
_MAX_BATCH_SIZE = 32
_MAX_INPUT_TOKENS = 8192
_DOCUMENT_PREFIX = "search_document: "
_DEFAULT_LOAD_TIMEOUT_SECONDS = 120.0


class _TextEmbeddingImpl(Protocol):
    """Minimal structural shape this module needs from `fastembed.TextEmbedding`."""

    def embed(self, texts: list[str]) -> Iterable[Iterable[float]]:
        """Embeds `texts`, yielding one vector-like sequence per input, in order."""
        ...


class FastEmbedEmbedder(EmbedderAdapter):
    """Local, credentials-free `EmbedderAdapter`. Thin wrap of `fastembed`.

    Never implements embedding algorithms itself — `fastembed` owns model loading and
    ONNX inference; this class only adapts its shape to `EmbedderAdapter`.
    """

    def __init__(
        self,
        model: str = _DEFAULT_MODEL,
        *,
        cache_dir: str | None = None,
        load_timeout_seconds: float = _DEFAULT_LOAD_TIMEOUT_SECONDS,
        _text_embedding: _TextEmbeddingImpl | None = None,
    ) -> None:
        """Initializes the adapter, loading the underlying ONNX model.

        Args:
            model: The `fastembed` model name to load. Defaults to
                `"nomic-embed-text-v1.5"`.
            cache_dir: Optional override for `fastembed`'s on-disk model cache
                directory.
            load_timeout_seconds: Bound on the model-load call, which may download
                ONNX weights (~274MB for the default model) from the network on first
                use. Defaults to 120 seconds.
            _text_embedding: Internal test-injection seam standing in for the loaded
                `fastembed.TextEmbedding` instance; production callers should leave
                this `None` (real model loading is attempted instead).

        Raises:
            EmbedderError: `EMBEDDER_DEPENDENCY_MISSING` (PERMANENT) if `fastembed` is
                not installed, or `EMBEDDER_TIMEOUT` (TRANSIENT) if model loading does
                not finish within `load_timeout_seconds`.
        """
        self._model = model
        self._impl = _text_embedding or _load_text_embedding(
            model, cache_dir, load_timeout_seconds
        )

    @property
    def model_name(self) -> str:
        """See `EmbedderAdapter.model_name`."""
        return self._model

    @property
    def native_dimensions(self) -> int:
        """See `EmbedderAdapter.native_dimensions`."""
        return _NATIVE_DIMENSIONS

    @property
    def max_batch_size(self) -> int:
        """See `EmbedderAdapter.max_batch_size`."""
        return _MAX_BATCH_SIZE

    @property
    def max_input_tokens(self) -> int:
        """See `EmbedderAdapter.max_input_tokens`."""
        return _MAX_INPUT_TOKENS

    @property
    def supports_mrl(self) -> bool:
        """See `EmbedderAdapter.supports_mrl`. Always `True` for this model."""
        return True

    @property
    def document_prefix(self) -> str | None:
        """See `EmbedderAdapter.document_prefix`. Always `"search_document: "`."""
        return _DOCUMENT_PREFIX

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """See `EmbedderAdapter.embed_batch`.

        No in-adapter retry: local CPU/ONNX inference has no rate limits, upstream
        service, or network timeout to retry against (unlike `AzureOpenAIEmbedder`).

        Args:
            texts: The texts to embed.

        Returns:
            One vector per input text, in the same order.

        Raises:
            Exception: Any exception `fastembed.TextEmbedding.embed` itself raises
                propagates unchanged for `Embedder` to wrap as `EMBEDDER_INTERNAL`.
        """
        return [
            [float(value) for value in vector] for vector in self._impl.embed(texts)
        ]


def _load_text_embedding(
    model: str, cache_dir: str | None, load_timeout_seconds: float
) -> _TextEmbeddingImpl:
    """Lazily imports `fastembed` and loads its `TextEmbedding` model.

    The load call is bounded by `load_timeout_seconds` since it may download ONNX
    model weights from the network on first use (an external call, per house rules).

    Args:
        model: The `fastembed` model name to load.
        cache_dir: Optional override for `fastembed`'s on-disk model cache directory.
        load_timeout_seconds: Bound on the model-load call.

    Returns:
        The loaded `fastembed.TextEmbedding` instance.

    Raises:
        EmbedderError: `EMBEDDER_DEPENDENCY_MISSING` (PERMANENT) if `fastembed` is not
            installed, or `EMBEDDER_TIMEOUT` (TRANSIENT) if the load does not finish
            within `load_timeout_seconds`.
    """
    try:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]
    except ImportError as exc:
        raise EmbedderError(
            "",
            "fastembed is not installed; install the 'local' extra "
            "(pip install vestibule[local]) to use FastEmbedEmbedder",
            error_code=EMBEDDER_DEPENDENCY_MISSING,
        ) from exc

    # Not a context manager: on timeout we must return control to the caller
    # immediately, not block in `shutdown(wait=True)` for a still-running download.
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(TextEmbedding, model_name=model, cache_dir=cache_dir)
    try:
        result: _TextEmbeddingImpl = future.result(timeout=load_timeout_seconds)
    except FutureTimeoutError as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        raise EmbedderError(
            "",
            f"loading fastembed model {model!r} did not finish within "
            f"{load_timeout_seconds}s (likely a stalled model-weight download)",
            error_code=EMBEDDER_TIMEOUT,
        ) from exc
    executor.shutdown(wait=False)
    return result
