"""Unit tests for AzureOpenAIEmbedder (REQ-007).

No live API calls — every test uses a recorded-response fake client in place of the
real `openai.AzureOpenAI`, monkeypatching the adapter's private `_client` attribute
(same convention as REQ-005's `test_docint_parser.py`). Every retry-loop test injects a
no-op `_sleep` so retry/backoff tests run instantly (hermetic, no real waiting).
"""

from __future__ import annotations

from typing import Any

import httpx2
import pytest
from openai import APITimeoutError, InternalServerError, RateLimitError

from ingestion.embedder.adapters.azure_openai import AzureOpenAIEmbedder
from ingestion.embedder.model import (
    EMBEDDER_RATE_LIMITED,
    EMBEDDER_TIMEOUT,
    EMBEDDER_UPSTREAM_ERROR,
    EmbedderError,
)


def _response(
    status_code: int, headers: dict[str, str] | None = None
) -> httpx2.Response:
    request = httpx2.Request("POST", "https://fake.example.com/embeddings")
    return httpx2.Response(status_code, headers=headers or {}, request=request)


def _rate_limit_error(retry_after: str | None = None) -> RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    return RateLimitError("rate limited", response=_response(429, headers), body=None)


def _internal_server_error() -> InternalServerError:
    return InternalServerError("server error", response=_response(503), body=None)


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(request=httpx2.Request("POST", "https://fake.example.com"))


class _FakeEmbeddingItem:
    def __init__(self, embedding: list[float]) -> None:
        self.embedding = embedding


class _FakeEmbeddingsResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [_FakeEmbeddingItem(v) for v in vectors]


class _FakeEmbeddingsResource:
    """Recorded-response fake standing in for `client.embeddings`."""

    def __init__(
        self,
        *,
        vectors: list[list[float]] | None = None,
        error_sequence: list[Exception | None] | None = None,
    ) -> None:
        self._vectors = vectors or []
        self._error_sequence = list(error_sequence or [])
        self.calls = 0

    def create(
        self, *, model: str, input: list[str], **kwargs: Any
    ) -> _FakeEmbeddingsResponse:
        self.calls += 1
        if self._error_sequence:
            error = self._error_sequence.pop(0)
            if error is not None:
                raise error
        return _FakeEmbeddingsResponse(self._vectors)


class _FakeClient:
    def __init__(self, embeddings: _FakeEmbeddingsResource) -> None:
        self.embeddings = embeddings


def _make_adapter(
    embeddings: _FakeEmbeddingsResource, *, max_attempts: int = 3
) -> AzureOpenAIEmbedder:
    adapter = AzureOpenAIEmbedder(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        api_version="2024-02-01",
        max_attempts=max_attempts,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        _sleep=lambda _seconds: None,
    )
    adapter._client = _FakeClient(embeddings)  # type: ignore[assignment]
    return adapter


# --- declared capabilities -------------------------------------------------------------------


def test_default_capabilities() -> None:
    adapter = _make_adapter(_FakeEmbeddingsResource())
    assert adapter.model_name == "text-embedding-3-large"
    assert adapter.native_dimensions == 3072
    assert adapter.max_batch_size == 96
    assert adapter.max_input_tokens == 8191
    assert adapter.supports_mrl is True
    assert adapter.document_prefix is None


def test_client_disables_its_own_internal_retries() -> None:
    adapter = AzureOpenAIEmbedder(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        api_version="2024-02-01",
    )
    assert adapter._client.max_retries == 0


# --- happy path -------------------------------------------------------------------------------


def test_embed_batch_returns_vectors_in_order() -> None:
    embeddings = _FakeEmbeddingsResource(vectors=[[0.1, 0.2], [0.3, 0.4]])
    adapter = _make_adapter(embeddings)

    result = adapter.embed_batch(["a", "b"])

    assert result == [[0.1, 0.2], [0.3, 0.4]]
    assert embeddings.calls == 1


# --- retry then success -----------------------------------------------------------------------


def test_embed_batch_retries_transient_failure_then_succeeds() -> None:
    embeddings = _FakeEmbeddingsResource(
        vectors=[[1.0]], error_sequence=[_rate_limit_error()]
    )
    adapter = _make_adapter(embeddings, max_attempts=3)

    result = adapter.embed_batch(["a"])

    assert result == [[1.0]]
    assert embeddings.calls == 2


# --- retries exhausted -> declared TRANSIENT codes ---------------------------------------------


def test_embed_batch_rate_limited_exhausted_raises_embedder_rate_limited() -> None:
    embeddings = _FakeEmbeddingsResource(
        error_sequence=[_rate_limit_error(), _rate_limit_error(), _rate_limit_error()]
    )
    adapter = _make_adapter(embeddings, max_attempts=3)

    with pytest.raises(EmbedderError) as exc_info:
        adapter.embed_batch(["a"])

    assert exc_info.value.error_code == EMBEDDER_RATE_LIMITED
    assert embeddings.calls == 3


def test_embed_batch_upstream_5xx_exhausted_raises_embedder_upstream_error() -> None:
    embeddings = _FakeEmbeddingsResource(
        error_sequence=[_internal_server_error(), _internal_server_error()]
    )
    adapter = _make_adapter(embeddings, max_attempts=2)

    with pytest.raises(EmbedderError) as exc_info:
        adapter.embed_batch(["a"])

    assert exc_info.value.error_code == EMBEDDER_UPSTREAM_ERROR


def test_embed_batch_timeout_exhausted_raises_embedder_timeout() -> None:
    embeddings = _FakeEmbeddingsResource(
        error_sequence=[_timeout_error(), _timeout_error()]
    )
    adapter = _make_adapter(embeddings, max_attempts=2)

    with pytest.raises(EmbedderError) as exc_info:
        adapter.embed_batch(["a"])

    assert exc_info.value.error_code == EMBEDDER_TIMEOUT


def test_embed_batch_unmapped_exception_propagates_unchanged() -> None:
    embeddings = _FakeEmbeddingsResource(error_sequence=[ValueError("bad request")])
    adapter = _make_adapter(embeddings, max_attempts=3)

    with pytest.raises(ValueError, match="bad request"):
        adapter.embed_batch(["a"])


# --- Retry-After header handling ----------------------------------------------------------------


def test_embed_batch_honors_retry_after_header_over_exponential_backoff() -> None:
    embeddings = _FakeEmbeddingsResource(
        vectors=[[1.0]], error_sequence=[_rate_limit_error(retry_after="7.5")]
    )
    observed_delays: list[float] = []
    adapter = AzureOpenAIEmbedder(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        api_version="2024-02-01",
        max_attempts=3,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        _sleep=observed_delays.append,
    )
    adapter._client = _FakeClient(embeddings)  # type: ignore[assignment]

    adapter.embed_batch(["a"])

    assert observed_delays == [7.5]


def test_embed_batch_falls_back_to_exponential_backoff_when_no_retry_after_header() -> (
    None
):
    embeddings = _FakeEmbeddingsResource(
        vectors=[[1.0]], error_sequence=[_rate_limit_error()]
    )
    observed_delays: list[float] = []
    adapter = AzureOpenAIEmbedder(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        api_version="2024-02-01",
        max_attempts=3,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        _sleep=observed_delays.append,
    )
    adapter._client = _FakeClient(embeddings)  # type: ignore[assignment]

    adapter.embed_batch(["a"])

    assert len(observed_delays) == 1
    assert observed_delays[0] != 7.5
    assert 0.0 <= observed_delays[0] <= 0.02


def test_embed_batch_falls_back_to_exponential_backoff_when_retry_after_header_malformed() -> (
    None
):
    embeddings = _FakeEmbeddingsResource(
        vectors=[[1.0]], error_sequence=[_rate_limit_error(retry_after="not-a-number")]
    )
    observed_delays: list[float] = []
    adapter = AzureOpenAIEmbedder(
        endpoint="https://fake.example.com",
        api_key="fake-key",
        api_version="2024-02-01",
        max_attempts=3,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
        _sleep=observed_delays.append,
    )
    adapter._client = _FakeClient(embeddings)  # type: ignore[assignment]

    adapter.embed_batch(["a"])

    assert len(observed_delays) == 1
    assert 0.0 <= observed_delays[0] <= 0.02
