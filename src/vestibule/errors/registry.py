"""Failure Taxonomy — Contract #4 canonical definition.

Every error in the framework is classified PERMANENT (never retry, mark the document
failed) or TRANSIENT (raise so a caller can retry with backoff, eventually poison).
This module owns the classification decision only: retry execution and poison-queue
integration are explicit callers' concerns, out of scope here (see the story).

``ErrorRegistry`` is a thread-safe, in-process, in-memory catalog mapping an error code
to its fixed ``Severity`` and a human-readable description. Every framework module
registers its own error code(s) into the process-wide ``_DEFAULT_REGISTRY`` singleton at
import time via the module-level ``register_error`` function; ``classify`` and
``all_codes`` read from that same singleton. ``RaggedError`` is the common base class
every framework exception inherits from, carrying ``error_code: str`` and a convenience
``severity`` property.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from enum import Enum
from typing import Literal

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    """PERMANENT | TRANSIENT — Contract #4's binary classification.

    Fixed, code-defined; the sole authority for accepted values (same pattern as
    ``Status`` (REQ-003), ``TrustTier`` (REQ-001)).
    """

    PERMANENT = "PERMANENT"
    TRANSIENT = "TRANSIENT"


@dataclass(frozen=True)
class ErrorCode:
    """One registered entry — code, its fixed severity, and a description.

    Attributes:
        code: The registry key, non-empty.
        severity: Fixed at registration time, never updated.
        description: Non-empty, human-readable, for observability/docs (AC6).
    """

    code: str
    severity: Severity
    description: str


class RaggedError(Exception):
    """Base exception for every framework error.

    Carries ``error_code: str`` (story AC5) — see the LLD's Assumption A1 for how this
    coexists with existing subclasses' pre-existing ``code`` class attribute.
    """

    def __init__(self, message: str, *, error_code: str) -> None:
        """Initializes the exception.

        Args:
            message: Human-readable error message.
            error_code: Always caller-supplied by the concrete subclass's own
                constructor (typically ``self.code``, its own fixed class attribute) —
                ``RaggedError`` itself defines no fixed code, since it is a base class
                shared by every module.
        """
        self.error_code = error_code
        super().__init__(message)

    @property
    def severity(self) -> Severity:
        """Convenience accessor for this error's classification.

        Always resolves via ``classify(self.error_code)`` against the module-level
        ``_DEFAULT_REGISTRY`` specifically — never against any local/isolated
        ``ErrorRegistry`` instance that may have raised this exception (Assumption A6).
        Callers needing severity from a local/isolated registry must call
        ``registry.classify(code)`` directly on that instance instead.

        Returns:
            The ``Severity`` classified for ``self.error_code``. Never raises; unknown
            codes resolve to ``Severity.TRANSIENT`` with a logged warning, per
            ``classify()``'s own contract.
        """
        return classify(self.error_code)


class ErrorCodeRegistrationInvalid(RaggedError):
    """Raised by ``ErrorRegistry.register()`` for any invalid registration call.

    Covers a code already registered (AC2's "duplicate registration") and a malformed
    code/severity/description (Assumption A3). Always PERMANENT — every trigger is a
    caller/module bug at import time, never a transient condition (Assumption A4/A5).

    Attributes:
        code: Fixed error code, ``"ERROR_CODE_REGISTRATION_INVALID"``.
        classification: Fixed classification, always ``"PERMANENT"``.
        attempted_code: The code the caller tried to register.
        reason: Which check failed — e.g. ``"duplicate registration"``,
            ``"invalid code"``, ``"invalid severity"``, ``"invalid description"``.
        existing: The already-registered ``ErrorCode`` for ``attempted_code``, populated
            only for the duplicate-registration trigger (F1); ``None`` for
            malformed-input triggers (F2-F4).
    """

    code: str = "ERROR_CODE_REGISTRATION_INVALID"
    classification: Literal["PERMANENT"] = "PERMANENT"

    def __init__(
        self,
        attempted_code: str,
        reason: str,
        *,
        existing: ErrorCode | None = None,
    ) -> None:
        """Initializes the exception.

        Args:
            attempted_code: The code the caller tried to register.
            reason: Human-readable rejection reason.
            existing: The pre-existing ``ErrorCode`` for ``attempted_code``, populated
                only for the duplicate-registration trigger (F1); ``None`` for
                malformed-input triggers (F2-F4), where nothing was previously
                registered under that (invalid) code.
        """
        self.attempted_code = attempted_code
        self.reason = reason
        self.existing = existing
        super().__init__(f"{attempted_code}: {reason}", error_code=self.code)


class ErrorRegistry:
    """Thread-safe registry mapping error code -> ``ErrorCode``.

    Immutable per entry once registered (AC2) — no update/remove method exists; the only
    mutation is ``register()``. Backing store: ``dict[str, ErrorCode]``
    (insertion-ordered, per Python 3.7+ dict semantics) guarded by one ``threading.Lock``
    held for the full duration of each public method (coarse-grained, correct-under-
    concurrency — same pattern as REQ-003's ``InMemoryLedgerStore``; negligible
    contention expected, since registration only happens at import time, not per-
    document).
    """

    def __init__(self) -> None:
        """Initializes an empty registry."""
        self._codes: dict[str, ErrorCode] = {}
        self._lock = threading.Lock()

    def register(self, code: str, severity: Severity, description: str) -> ErrorCode:
        """Registers a new error code.

        Checks, in order: (1) ``code`` is already present in this registry (AC2) — if
        so, raises ``ErrorCodeRegistrationInvalid(code, "duplicate registration",
        existing=<prior ErrorCode>)`` immediately, before any other input is inspected;
        (2) ``code`` is a non-empty ``str`` (Assumption A3); (3) ``severity`` is a
        ``Severity`` member; (4) ``description`` is a non-empty ``str``. Each of checks
        2-4 raises ``ErrorCodeRegistrationInvalid`` (PERMANENT) with ``existing=None`` on
        failure. This order is mandated, not incidental: a call supplying both an
        already-registered ``code`` AND an invalid ``severity``/``description`` raises
        the duplicate-registration error (check 1) — checks 2-4 never run against a code
        that is already present. Thread-safe: the presence check and the write happen
        atomically under ``self._lock``, so two concurrent calls for the same code can
        never both succeed (AC7).

        Args:
            code: The error code to register.
            severity: The fixed ``Severity`` for this code.
            description: Human-readable description, for observability/docs.

        Returns:
            The newly-created ``ErrorCode`` on success.

        Raises:
            ErrorCodeRegistrationInvalid: If any of the checks above fails.
        """
        with self._lock:
            existing = self._codes.get(code)
            if existing is not None:
                raise ErrorCodeRegistrationInvalid(
                    code, "duplicate registration", existing=existing
                )
            if not isinstance(code, str) or not code:
                raise ErrorCodeRegistrationInvalid(code, "invalid code")
            if not isinstance(severity, Severity):
                raise ErrorCodeRegistrationInvalid(code, "invalid severity")
            if not isinstance(description, str) or not description:
                raise ErrorCodeRegistrationInvalid(code, "invalid description")
            entry = ErrorCode(code=code, severity=severity, description=description)
            self._codes[code] = entry
            return entry

    def classify(self, code: str) -> Severity:
        """Looks up ``code``'s severity.

        Returns ``Severity.TRANSIENT`` and logs a warning (module-level logger, WARNING
        level, message includes the unknown code) if ``code`` is not registered (AC3) —
        never raises, for any ``str`` input including malformed shapes. This is the
        literal implementation of CLAUDE.md's "unclassified errors default to
        TRANSIENT."

        Args:
            code: The error code to classify.

        Returns:
            The registered ``Severity`` for ``code``, or ``Severity.TRANSIENT`` if
            ``code`` is not registered.
        """
        with self._lock:
            entry = self._codes.get(code)
        if entry is None:
            logger.warning(
                "Unknown error code classified as TRANSIENT by default",
                extra={"error_code": code},
            )
            return Severity.TRANSIENT
        return entry.severity

    def all_codes(self) -> list[ErrorCode]:
        """Returns a snapshot list of every registered ``ErrorCode``.

        Returns:
            A new list, in registration order (AC6), on every call (no aliasing of
            internal state); ``[]`` for an empty registry.
        """
        with self._lock:
            return list(self._codes.values())


_DEFAULT_REGISTRY: ErrorRegistry = ErrorRegistry()
"""Process-wide singleton every framework module registers its own codes into at import
time (story: "a registry every module registers its error codes into at import time").
Unit tests exercising ``ErrorRegistry``'s own behavior MUST construct a fresh
``ErrorRegistry()`` instance instead (Assumption A2)."""


def register_error(code: str, severity: Severity, description: str) -> ErrorCode:
    """Registers ``code`` into the process-wide default registry.

    Args:
        code: The error code to register.
        severity: The fixed ``Severity`` for this code.
        description: Human-readable description, for observability/docs.

    Returns:
        The newly-created ``ErrorCode`` on success.

    Raises:
        ErrorCodeRegistrationInvalid: See ``ErrorRegistry.register``.
    """
    return _DEFAULT_REGISTRY.register(code, severity, description)


def classify(code: str) -> Severity:
    """Classifies ``code`` via the process-wide default registry.

    Args:
        code: The error code to classify.

    Returns:
        The registered ``Severity`` for ``code``, or ``Severity.TRANSIENT`` if ``code``
        is not registered. See ``ErrorRegistry.classify``.
    """
    return _DEFAULT_REGISTRY.classify(code)


def all_codes() -> list[ErrorCode]:
    """Returns a snapshot of every code registered in the process-wide default registry.

    Returns:
        A new list of every registered ``ErrorCode``, in registration order.
    """
    return _DEFAULT_REGISTRY.all_codes()


# Bootstrap — this module registers its own error code into the same default registry
# it exposes, at import time, following the exact pattern every consumer module uses for
# its own code. Not recursive: this is the first-ever registration call in a fresh
# process, so the registry is empty and this call always succeeds.
register_error(
    ErrorCodeRegistrationInvalid.code,
    Severity.PERMANENT,
    "an error code was registered with an invalid code/severity/description, or was "
    "already registered (registry is immutable after first registration)",
)
