"""EmbedderAdapter contract (REQ-007).

`EmbedderAdapter` is the thin-wrapper contract every embedding backend implements
(house rules: "adapters thin" — implementations wrap an underlying SDK/library, never
implement embedding algorithms themselves). Adapters declare their own capabilities
(batch size, native dimensionality, MRL support, task-type prefix) via read-only
properties; the `Embedder` orchestrator reads and respects them, never hard-codes a
per-adapter special case.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class EmbedderAdapter(ABC):
    """Thin wrapper contract for a single embedding backend.

    Implementations wrap an underlying SDK or managed service, never implementing
    embedding algorithms themselves.
    """

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embeds one batch of texts, in order.

        Args:
            texts: The texts to embed, already prefixed by the caller (`Embedder`) if
                `document_prefix` is not `None`. `len(texts)` is guaranteed by the
                caller to be `<= max_batch_size`.

        Returns:
            One vector per input text, in the same order, each of length
            `native_dimensions`. MRL truncation/re-normalization, if configured, is the
            caller's (`Embedder`'s) responsibility, not this method's.

        Raises:
            EmbedderError: `EMBEDDER_RATE_LIMITED`, `EMBEDDER_UPSTREAM_ERROR`, or
                `EMBEDDER_TIMEOUT` (all TRANSIENT), after exhausting this adapter's own
                in-adapter retries. Any other exception may propagate unchanged —
                `Embedder` wraps any such exception as `EMBEDDER_INTERNAL`.
        """
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def model_name(self) -> str:
        """The embedding model identifier, for `EmbeddedChunk.embedding_model`."""
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def native_dimensions(self) -> int:
        """The un-truncated vector length this adapter's model produces."""
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def max_batch_size(self) -> int:
        """The maximum number of texts this adapter accepts in one `embed_batch` call."""
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def max_input_tokens(self) -> int:
        """The maximum token count this adapter accepts for a single input text."""
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def supports_mrl(self) -> bool:
        """Whether this adapter's model supports Matryoshka (MRL) dimension truncation."""
        raise NotImplementedError  # pragma: no cover

    @property
    @abstractmethod
    def document_prefix(self) -> str | None:
        """The document-side task-type prefix this adapter requires, if any.

        `None` if this adapter's model requires no prefix (e.g. OpenAI embedding
        models). When not `None`, a matching query-side prefix must be applied by the
        query path (out of scope for this REQ) — a mismatched prefix scheme between
        ingest and query silently degrades retrieval with no error.
        """
        raise NotImplementedError  # pragma: no cover
