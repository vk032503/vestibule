"""AzureOpenAIEmbedder — thin EmbedderAdapter wrapping Azure OpenAI embeddings (REQ-007).

Production path. Default model `text-embedding-3-large` (3072 native dimensions,
Matryoshka-capable). In-adapter retry (exponential backoff + full jitter, respecting a
429 response's `Retry-After` header when present) is implemented here via `tenacity` —
`Embedder` itself performs no retry of its own (story: "in-adapter retry").
"""

from __future__ import annotations

import time
from typing import Callable

from openai import APITimeoutError, AzureOpenAI, InternalServerError, RateLimitError
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from vestibule.embedder.adapters.base import EmbedderAdapter
from vestibule.embedder.model import (
    EMBEDDER_RATE_LIMITED,
    EMBEDDER_TIMEOUT,
    EMBEDDER_UPSTREAM_ERROR,
    EmbedderError,
)

_DEFAULT_MODEL = "text-embedding-3-large"
_NATIVE_DIMENSIONS = 3072
_MAX_BATCH_SIZE = 96
_MAX_INPUT_TOKENS = 8191
_DOCUMENT_PREFIX = None

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_MAX_ATTEMPTS = 5
_DEFAULT_BACKOFF_BASE_SECONDS = 2.0
_DEFAULT_BACKOFF_MAX_SECONDS = 60.0

_RETRYABLE_EXCEPTIONS = (RateLimitError, InternalServerError, APITimeoutError)


class AzureOpenAIEmbedder(EmbedderAdapter):
    """Production `EmbedderAdapter`. Thin wrap of Azure OpenAI's embeddings endpoint.

    `endpoint`/`api_key` are injected by the composition root from env only (house
    rules: no secrets in code); this class never reads env itself (same pattern as
    REQ-005's `DocumentIntelligenceParser`).
    """

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        api_version: str,
        model: str = _DEFAULT_MODEL,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = _DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = _DEFAULT_BACKOFF_MAX_SECONDS,
        _sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Initializes the adapter with an Azure OpenAI client.

        Args:
            endpoint: The Azure OpenAI resource endpoint URL. Read by the composition
                root from `AZURE_OPENAI_ENDPOINT` (see `config/embedder.yaml`
                `azure_openai.endpoint_env_var`) — never read here.
            api_key: The Azure OpenAI API key. Read by the composition root from
                `AZURE_OPENAI_API_KEY` — never read here.
            api_version: The Azure OpenAI REST API version to target.
            model: The embedding model deployment to call. Defaults to
                `"text-embedding-3-large"`.
            timeout_seconds: Per-call timeout passed to every `embeddings.create` call.
            max_attempts: Maximum `embed_batch` attempts (first call plus retries)
                before raising a TRANSIENT `EmbedderError`.
            backoff_base_seconds: Exponential-backoff-with-full-jitter multiplier
                between retries, when no `Retry-After` header is present.
            backoff_max_seconds: Cap on the computed backoff delay.
            _sleep: Internal test-injection seam for the retry loop's sleep function;
                production callers should leave this at its default (`time.sleep`).
        """
        self._client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
            max_retries=0,
        )
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleep = _sleep

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
        """See `EmbedderAdapter.supports_mrl`. Always `True` for this model family."""
        return True

    @property
    def document_prefix(self) -> str | None:
        """See `EmbedderAdapter.document_prefix`. Always `None` for OpenAI models."""
        return _DOCUMENT_PREFIX

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """See `EmbedderAdapter.embed_batch`.

        Retries `RateLimitError`/`InternalServerError`/`APITimeoutError` via `tenacity`
        (exponential backoff with full jitter, honoring a `RateLimitError`'s
        `Retry-After` header when present) up to `max_attempts` times before mapping
        the exhausted failure to its declared TRANSIENT `EmbedderError`.

        Args:
            texts: The texts to embed.

        Returns:
            One vector per input text, in the same order.

        Raises:
            EmbedderError: `EMBEDDER_RATE_LIMITED`, `EMBEDDER_UPSTREAM_ERROR`, or
                `EMBEDDER_TIMEOUT` (all TRANSIENT), once retries are exhausted. Any
                other exception propagates unchanged for `Embedder` to wrap as
                `EMBEDDER_INTERNAL`.
        """
        retrying: Retrying = Retrying(
            sleep=self._sleep,
            stop=stop_after_attempt(self._max_attempts),
            wait=_RetryAfterAwareWait(
                self._backoff_base_seconds, self._backoff_max_seconds
            ),
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
            reraise=True,
        )
        try:
            return retrying(self._call_api, texts)
        except RateLimitError as exc:
            raise EmbedderError("", str(exc), error_code=EMBEDDER_RATE_LIMITED) from exc
        except InternalServerError as exc:
            raise EmbedderError(
                "", str(exc), error_code=EMBEDDER_UPSTREAM_ERROR
            ) from exc
        except APITimeoutError as exc:
            raise EmbedderError("", str(exc), error_code=EMBEDDER_TIMEOUT) from exc

    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """One un-retried call to the Azure OpenAI embeddings endpoint.

        Args:
            texts: The texts to embed.

        Returns:
            One vector per input text, in the same order.
        """
        response = self._client.embeddings.create(
            model=self._model, input=texts, timeout=self._timeout_seconds
        )
        return [item.embedding for item in response.data]


class _RetryAfterAwareWait:
    """`tenacity` wait strategy honoring a `RateLimitError`'s `Retry-After` header.

    Falls back to exponential backoff with full jitter (`wait_random_exponential`) for
    every other retryable exception, or when a 429 carries no usable `Retry-After`.
    """

    def __init__(self, base_seconds: float, max_seconds: float) -> None:
        """Initializes the fallback backoff strategy.

        Args:
            base_seconds: The fallback strategy's exponential multiplier.
            max_seconds: The fallback strategy's cap on the computed delay.
        """
        self._fallback = wait_random_exponential(
            multiplier=base_seconds, max=max_seconds
        )

    def __call__(self, retry_state: RetryCallState) -> float:
        """Computes the delay before the next retry attempt.

        Args:
            retry_state: The current `tenacity` retry state.

        Returns:
            The `Retry-After` value in seconds if `retry_state`'s outcome is a
            `RateLimitError` carrying a parseable `Retry-After` header; otherwise the
            fallback exponential-backoff-with-jitter delay.
        """
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None else None
        if isinstance(exc, RateLimitError):
            retry_after = _parse_retry_after(exc)
            if retry_after is not None:
                return retry_after
        return float(self._fallback(retry_state))


def _parse_retry_after(exc: RateLimitError) -> float | None:
    """Parses a `RateLimitError`'s `Retry-After` header, if present and well-formed.

    Args:
        exc: The `RateLimitError` to inspect.

    Returns:
        The header's value in seconds, or `None` if absent or not a valid number.
    """
    value = exc.response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None
