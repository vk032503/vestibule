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

### Changed
- _(none yet)_

### Removed
- _(none yet)_

### Fixed
- _(none yet)_

### Security
- _(none yet)_