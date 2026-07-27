# Changelog

All notable changes documented here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added
- REQ-001: Arrival envelope (`DocumentEnvelope`) — canonical shape for pipeline entry.
- REQ-002: Deterministic identity — `derive_doc_id`, `derive_chunk_id`, `IdentityInvalid`.
- REQ-003: State ledger (`ingestion.ledger`) — `Status` FSM, `LedgerRow`, `LedgerStore`
  port, `InMemoryLedgerStore`, `LedgerTransitionInvalid` (PERMANENT,
  `LEDGER_TRANSITION_INVALID`), and `is_benign_concurrent_loss` for triaging losing
  callers of a concurrent `transition()` race — Contract #3.
- REQ-004: Failure taxonomy (`ingestion.errors`) — `Severity` (PERMANENT/TRANSIENT),
  `ErrorCode`, `ErrorRegistry`, `RaggedError` base exception carrying `error_code`, and
  module-level `register_error`/`classify`/`all_codes` — Contract #4. `classify()`
  defaults unregistered codes to `TRANSIENT` with a logged warning.

### Changed
- REQ-004: `IdentityInvalid`, `EnvelopeValidationError`, and `LedgerTransitionInvalid`
  (REQ-001/002/003) now inherit from `RaggedError` and register their codes
  (`IDENTITY_INVALID`, `ENVELOPE_INVALID`, `LEDGER_TRANSITION_INVALID`) into the default
  error registry at import time. Non-breaking: no existing public constructor signature
  or attribute changed.

### Removed
- _(none yet)_

### Fixed
- _(none yet)_

### Security
- _(none yet)_