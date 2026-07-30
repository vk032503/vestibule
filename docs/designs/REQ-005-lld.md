# REQ-005 — Document Analyzer — LLD

**Story:** docs/stories/REQ-005.md · **Phase:** 2 · **Pipeline:** full

## Revision note
Revised after a design-review REJECT — two findings, both addressed in this revision:

1. **[BLOCKER]** §3's failure paths F3–F6 (`PARSER_TIMEOUT`, `DOCINT_RATE_LIMITED`,
   `DOCINT_UPSTREAM_ERROR`, `PARSER_INTERNAL` — all TRANSIENT) previously terminalized the ledger
   row to `Status.FAILED` on every transient failure. This contradicted the story's own AC5
   ("records the failure without transitioning to a terminal state") and defeated REQ-003's retry
   mechanism: `Status.FAILED` is terminal (`_LEGAL_TRANSITIONS[Status.FAILED] = frozenset()`), so a
   genuine subsequent retry would hit `LedgerTransitionInvalid`, get triaged as non-benign by
   `is_benign_concurrent_loss` (`attempted_to_status=ANALYZING`, `observed=FAILED` → `False`), and
   re-raise — permanently stranding the `doc_id` with `attempts` frozen (REQ-003 Assumption A4),
   making Contract #4's "poison after max dequeue" impossible to implement. **Fixed:** F3–F6 now
   perform a same-status self-transition (`analyzing → analyzing`, REQ-003 Assumption A2),
   recording `error_code`/`error_message` and incrementing `attempts`, leaving the row in
   `analyzing`. Only F1/F2 (PERMANENT) terminalize the row to `failed`. §3, §4, and §7 updated
   accordingly.
2. **[MINOR]** The story's "Market-awareness note" (a `benchmarks/` folder + README as a PR
   deliverable) was unaddressed by the prior revision. Added as Assumption A8 below — scoped as a
   documentation-only PR deliverable, not a designed interface, consistent with how this LLD flags
   its other scope decisions.

## Assumptions (non-blocking, flagged per house rules — same pattern as REQ-004)

The story specifies `detect_type`, `ParserAdapter`, `Analyzer.analyze`, the two adapters, and the
six error codes fully enough to design against, but leaves the following unstated. Following the
precedent set by REQ-001–004, these are treated as scoped design decisions, documented here, not
open questions that block the LLD:

- **A1 — `bytes_reader` has no defined interface in the story.** This LLD introduces a minimal
  `BytesReader` `Protocol` (`read(size: int = -1) -> bytes`, `seek(offset: int, whence: int = 0) ->
  int`) — the smallest surface both parsers and the type-detector need (random access for magic-byte
  sniffing + PDF page probing). Whatever component fetches bytes from `envelope.blob_path` (a
  storage adapter, out of scope for this REQ, likely REQ-006/coordinator territory) must hand the
  Analyzer something satisfying this Protocol — e.g. `io.BytesIO` or a seekable blob-stream wrapper.
- **A2 — ledger-row pre-existence and redelivery/idempotency triage.** The story's `Analyzer.analyze`
  orchestration (step 1: "transition ledger `pending → analyzing`") presupposes a ledger row already
  exists in `pending` (created by `LedgerStore.create()`, REQ-003, by an upstream coordinator out of
  scope here). Under Contract #2's at-least-once-delivery assumption, a redelivered `analyze()` call
  for a `doc_id` that has *already* progressed past `analyzing` (e.g. a worker crashed after this
  REQ's own `analyzing → chunking` write but before acking the queue message) would otherwise see
  its own `pending → analyzing` transition rejected as illegal (current status is `chunking`, not
  `pending`) and incorrectly treat a benign replay as a hard failure. This LLD requires `Analyzer
  .analyze` to catch `LedgerTransitionInvalid` on its *first* transition specifically, triage it via
  REQ-003's `is_benign_concurrent_loss(exc.observed_row, Status.ANALYZING, Status.ANALYZING)`, and on
  a benign result, skip straight to re-running `detect_type`/`parser.parse` and returning the
  `Element` list **without** attempting any further ledger write (the row is already at or past
  `chunking`; nothing to record) — satisfying the story's own Identity & Idempotency touchpoint
  ("re-analyzing the same `doc_id` produces the same set of elements... deterministic parsing").
  A non-benign `LedgerTransitionInvalid` (e.g. row genuinely `failed`, or unknown `doc_id`) is
  re-raised unchanged — it is not this module's error code, and not this module's bug to swallow.
  Note this triage path is distinct from a redelivery that lands *while the row is still
  `analyzing`* (e.g. after an F3–F6 self-transition, below) — that case never raises
  `LedgerTransitionInvalid` at all, since `to_status == current.status` is itself a legal
  same-status transition (REQ-003 Assumption A2); see §3 step 2's note.
- **A3 — `AnalyzerError` is a single class with a caller-supplied `error_code`, not six subclasses.**
  REQ-004's retrofitted classes (`EnvelopeValidationError`, `IdentityInvalid`,
  `LedgerTransitionInvalid`) each fix one `code` as a class attribute. This story's AC3/AC4 instead
  both read "raises `AnalyzerError` carrying `<code>`" — one exception type, varying `error_code` per
  raise site — so this LLD implements `AnalyzerError(RaggedError)` with `error_code: str` supplied at
  construction (mirroring `RaggedError`'s own base-class shape), not six fixed-code subclasses.
- **A4 — content_hash verification of `bytes_reader` against `envelope.content_hash` is out of
  scope for the Analyzer.** The story does not mention it, and Contract #1 already requires the
  envelope's `content_hash` to have been validated at arrival (REQ-001). Re-verifying that the bytes
  handed to `analyze()` still match `content_hash` is the responsibility of whichever component
  fetches those bytes from `blob_path` (out of scope here) — flagged so the scope boundary is
  explicit, not because it is ambiguous.
- **A5 — per-call timeout is thread-based; Python cannot forcibly kill a running thread.** `Analyzer`
  wraps each `parser.parse()` call in a `concurrent.futures.ThreadPoolExecutor` and calls
  `future.result(timeout=parser_timeout_seconds)`. On timeout, `Analyzer` raises `AnalyzerError`
  (`PARSER_TIMEOUT`, TRANSIENT) and abandons the future — the underlying parser thread (e.g. a stuck
  network call inside `DocumentIntelligenceParser`) may continue running in the background until it
  itself completes or errors; this is a documented, standard-library limitation, not a defect
  introduced by this design. Adapters remain thin: the timeout wrapper lives in `Analyzer`, not
  duplicated per-adapter.
- **A6 — the three numeric config tunables are real runtime defaults, not documentation-only.**
  Unlike REQ-001–004's numeric config entries (`content_hash_length`, `default_list_limit`,
  etc. — all "documentation only, code is authoritative," because none of them are
  safety/idempotency-critical invariants), `scanned_pdf_probe_pages`,
  `scanned_pdf_min_chars_per_page`, and `parser_timeout_seconds` are ordinary *operational* tunables
  with no bearing on Contract #2/#3 correctness (same category as `default_list_limit`, REQ-003
  §6). This LLD expresses them as `AnalyzerConfig` dataclass field defaults (code-authoritative,
  since no config-loading mechanism exists anywhere in this codebase yet — REQ-003 §6 precedent) and
  keeps `config/analyzer.yaml` in sync via a dedicated test, exactly as `default_list_limit` was.
- **A7 — enum string values are lowercase-snake, matching `Status`/`TrustTier` precedent.** The story
  writes `DetectedType`/`ElementType` members in upper-case prose (`DIGITAL_PDF`, `TABLE`, ...); this
  LLD keeps those as the Python enum *member names* but gives each a lowercase-snake string *value*
  (`DetectedType.DIGITAL_PDF = "digital_pdf"`), matching every prior enum in this codebase
  (`Status.PENDING = "pending"`, `TrustTier.PUBLIC = "public"`).
- **A8 — the story's Market-awareness note (`benchmarks/` folder + README) is a PR-level
  deliverable, not a designed interface.** It defines no class, function, error code, ledger state,
  or contract surface, so it is scoped here rather than modeled in §1/§2. This LLD requires the PR
  implementing it to include `benchmarks/README.md` covering: (a) what a future parser-adapter
  benchmark run measures (extraction accuracy against a labeled fixture set, latency, cost per
  page); (b) how a candidate adapter (Docling, Unstructured, a VLM-based parser, or a newer entrant)
  is registered into `ParserRegistry` for a benchmark run without touching `Analyzer` — the exact
  swap-in mechanism AC6 already requires and tests; and (c) an explicit statement that no
  benchmark-running automation, scoring harness, or CI job is being built by this REQ — the folder
  and README are scaffolding/market-tracking commitment only, per the story's own framing. No code
  in this LLD depends on `benchmarks/` existing; it carries no runtime behavior.

## 1. Interfaces

```python
# ingestion/analyzer/model.py

class DetectedType(str, Enum):
    """Sole authority for accepted detected-type values (A7)."""
    DIGITAL_PDF = "digital_pdf"
    SCANNED_PDF = "scanned_pdf"
    DOCX = "docx"
    IMAGE = "image"
    UNSUPPORTED = "unsupported"


class ElementType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    LIST = "list"
    IMAGE_CAPTION = "image_caption"
    CODE = "code"


@dataclass(frozen=True)
class Element:
    """One structured unit extracted from a document. Immutable, adapter-output-only —
    never constructed by Analyzer itself, only by ParserAdapter implementations."""
    type: ElementType
    text: str
    metadata: dict[str, Any]
    confidence: float | None = None


class BytesReader(Protocol):
    """Minimal seekable-byte-source interface (Assumption A1). Satisfied by io.BytesIO,
    a wrapped blob stream, or any equivalent. No parsing semantics — pure I/O surface."""
    def read(self, size: int = -1) -> bytes: ...
    def seek(self, offset: int, whence: int = 0) -> int: ...


class AnalyzerError(RaggedError):
    """Single exception type for every Analyzer-raised failure; error_code varies per
    raise site (Assumption A3) — unlike REQ-004's fixed-code-per-class subclasses."""

    def __init__(
        self,
        doc_id: str,
        reason: str,
        *,
        error_code: str,
        detected_type: DetectedType | None = None,
    ) -> None:
        self.doc_id = doc_id
        self.reason = reason
        self.detected_type = detected_type
        super().__init__(f"{doc_id}: {reason}", error_code=error_code)


# Registered at import time — Contract #4 (see §5 for full table).
register_error(ANALYZER_UNSUPPORTED_TYPE, Severity.PERMANENT, "...")
register_error(ANALYZER_NO_PARSER, Severity.PERMANENT, "...")
register_error(PARSER_TIMEOUT, Severity.TRANSIENT, "...")
register_error(DOCINT_RATE_LIMITED, Severity.TRANSIENT, "...")
register_error(DOCINT_UPSTREAM_ERROR, Severity.TRANSIENT, "...")
register_error(PARSER_INTERNAL, Severity.TRANSIENT, "...")
```

```python
# ingestion/analyzer/registry.py

class ParserAdapter(ABC):
    """Thin wrapper contract — house rules ("adapters thin"): implementations wrap an
    underlying library/service, never implement parsing/OCR algorithms themselves."""

    @abstractmethod
    def parse(self, envelope: ArrivalEnvelope, bytes_reader: BytesReader) -> list[Element]:
        """Raises AnalyzerError with a code from this module's registered set (or lets
        an unmapped exception propagate — Analyzer wraps it as PARSER_INTERNAL)."""
        raise NotImplementedError


class ParserRegistry:
    """DetectedType -> ParserAdapter lookup. No I/O; in-process dict."""

    def __init__(self) -> None: ...
    def register(self, detected_type: DetectedType, parser: ParserAdapter) -> None: ...
    def get(self, detected_type: DetectedType) -> ParserAdapter | None: ...
```

```python
# ingestion/analyzer/detect.py

def detect_type(
    envelope: ArrivalEnvelope,
    bytes_reader: BytesReader,
    *,
    scanned_pdf_probe_pages: int,
    scanned_pdf_min_chars_per_page: int,
) -> DetectedType:
    """Magic-byte sniff (never extension-based, per story) -> PDF/DOCX/IMAGE/UNSUPPORTED.
    For PDF: further probes scanned_pdf_probe_pages pages via a thin pymupdf open+
    get_text call (sniffing only, not the parse path PyMuPDFParser uses) and classifies
    SCANNED_PDF if avg chars/page < scanned_pdf_min_chars_per_page, else DIGITAL_PDF.
    Encrypted or corrupt PDFs, and any unrecognized magic bytes, resolve to UNSUPPORTED.
    Never raises: unrecognized/corrupt input is a return value, not an exception -
    Analyzer is the sole raise site for ANALYZER_UNSUPPORTED_TYPE."""
    ...
```

```python
# ingestion/analyzer/analyzer.py

@dataclass(frozen=True)
class AnalyzerConfig:
    """Operational tunables (Assumption A6) — code-authoritative defaults, kept in sync
    with config/analyzer.yaml via a dedicated test (§6/§7)."""
    scanned_pdf_probe_pages: int = 3
    scanned_pdf_min_chars_per_page: int = 50
    parser_timeout_seconds: float = 240.0


class Analyzer:
    """Orchestrates type detection, parser dispatch, and the analyzing ledger stage.
    Owns no parsing logic itself — delegates entirely to ParserAdapter implementations
    (house rules: "never parse documents ... in-house")."""

    def __init__(
        self, ledger: LedgerStore, registry: ParserRegistry, config: AnalyzerConfig
    ) -> None: ...

    def analyze(self, envelope: ArrivalEnvelope, bytes_reader: BytesReader) -> list[Element]:
        """See §3 for the full numbered sequence, including the A2 idempotent-replay path."""
        ...
```

```python
# ingestion/analyzer/parsers/pymupdf_parser.py

class PyMuPDFParser(ParserAdapter):
    """Routes for DIGITAL_PDF. Thin wrap of pymupdf (fitz) — extracts text with
    page/paragraph boundaries; maps blocks to Element(PARAGRAPH | HEADING, ...) via
    pymupdf's own block/font-size signals only, no custom layout algorithm."""

    def parse(self, envelope: ArrivalEnvelope, bytes_reader: BytesReader) -> list[Element]: ...
```

```python
# ingestion/analyzer/parsers/docint_parser.py

class DocumentIntelligenceParser(ParserAdapter):
    """Routes for SCANNED_PDF. Thin wrap of Azure Document Intelligence's
    prebuilt-layout model. endpoint/api_key are injected by the composition root from
    env only (house rules: no secrets in code); this class never reads env itself."""

    def __init__(
        self, endpoint: str, api_key: str, api_version: str, model_id: str = "prebuilt-layout"
    ) -> None: ...

    def parse(self, envelope: ArrivalEnvelope, bytes_reader: BytesReader) -> list[Element]:
        """Maps azure.core.exceptions.HttpResponseError: status_code == 429 ->
        AnalyzerError(DOCINT_RATE_LIMITED); status_code >= 500 ->
        AnalyzerError(DOCINT_UPSTREAM_ERROR); anything else unmapped propagates for
        Analyzer to wrap as PARSER_INTERNAL."""
        ...
```

## 2. Data model

| Type | Field | Type | Notes |
|---|---|---|---|
| `DetectedType` (str enum) | — | — | `digital_pdf \| scanned_pdf \| docx \| image \| unsupported` |
| `ElementType` (str enum) | — | — | `heading \| paragraph \| table \| list \| image_caption \| code` |
| `Element` (frozen dataclass) | `type` | `ElementType` | |
| | `text` | `str` | |
| | `metadata` | `dict[str, Any]` | adapter-specific (page number, bbox, row/col for tables, etc.) |
| | `confidence` | `float \| None` | populated by OCR/layout adapters; `None` for deterministic-text parsers |
| `AnalyzerError` | `doc_id` | `str` | |
| | `reason` | `str` | |
| | `error_code` | `str` | inherited from `RaggedError`; one of six registered codes (A3) |
| | `detected_type` | `DetectedType \| None` | populated where relevant (e.g. `ANALYZER_UNSUPPORTED_TYPE`, `ANALYZER_NO_PARSER`) |
| `AnalyzerConfig` (frozen dataclass) | `scanned_pdf_probe_pages` | `int` | default `3` |
| | `scanned_pdf_min_chars_per_page` | `int` | default `50` |
| | `parser_timeout_seconds` | `float` | default `240.0` |
| `ParserRegistry` | `_parsers` | `dict[DetectedType, ParserAdapter]` | private, in-process |

No new ledger/table row is owned by this module. `Analyzer` reads and writes existing `LedgerRow`
rows (REQ-003) via `LedgerStore`, keyed on `envelope.doc_id`; it introduces no new persistent state
of its own. `DocumentIntelligenceParser`'s `endpoint`/`api_key` are constructor-injected strings, not
stored in any dataclass/config file (house rules: secrets from env only).

## 3. Sequence

**Happy path**
1. Caller (out of scope — a coordinator/worker) has already validated `envelope` (REQ-001) and
   ensured a `LedgerRow` exists for `envelope.doc_id` in `pending` (REQ-003 `LedgerStore.create()`).
2. `Analyzer.analyze(envelope, bytes_reader)` calls `ledger.transition(envelope.doc_id,
   to_status=Status.ANALYZING, stage=Status.ANALYZING)`. Succeeds; row is now `analyzing`,
   `attempts = 1` (REQ-003 Assumption A4). Note: if this call is itself a redelivery arriving after
   a prior transient self-transition retry (F3–F6, below), the row is already `analyzing` —
   `to_status == current.status` is itself a legal same-status self-transition (REQ-003 Assumption
   A2), so this step succeeds directly (`attempts` increments again) without raising
   `LedgerTransitionInvalid`. No separate Analyzer-side handling is required for that case, and it
   is distinct from F7 below, which covers a row that has progressed *past* `analyzing`, not one
   still sitting in it.
3. `detect_type(envelope, bytes_reader, ...)` runs magic-byte sniffing (+ PDF sub-typing probe for
   PDF magic bytes); returns a `DetectedType`.
4. `registry.get(detected_type)` returns a registered `ParserAdapter`.
5. `Analyzer` calls `parser.parse(envelope, bytes_reader)` inside the timeout wrapper (A5); returns
   within `parser_timeout_seconds`.
6. `Analyzer` calls `ledger.transition(envelope.doc_id, to_status=Status.CHUNKING,
   stage=Status.CHUNKING)`. Row is now `chunking`.
7. `Analyzer.analyze` returns the `list[Element]` to the caller.

**Failure paths**
- F1. `detect_type` returns `DetectedType.UNSUPPORTED` (unrecognized magic bytes, encrypted PDF,
  corrupt file) → `Analyzer` calls `ledger.transition(..., to_status=Status.FAILED,
  stage=Status.FAILED, error_code=ANALYZER_UNSUPPORTED_TYPE)` — one of only two failure paths in
  this module that terminalizes the row (contrast the self-transitions in F3–F6, below) — then
  raises `AnalyzerError(doc_id, reason, error_code=ANALYZER_UNSUPPORTED_TYPE, detected_type=UNSUPPORTED)`.
  PERMANENT (AC3).
- F2. `registry.get(detected_type)` returns `None` (supported type, no adapter registered — a
  registry misconfiguration) → same terminalizing-transition pattern as F1, with
  `error_code=ANALYZER_NO_PARSER`; raises `AnalyzerError(..., error_code=ANALYZER_NO_PARSER)`.
  PERMANENT (AC4).
- F3. `parser.parse()` does not return within `parser_timeout_seconds` → `Analyzer` abandons the
  future (A5), calls `ledger.transition(envelope.doc_id, to_status=Status.ANALYZING,
  stage=Status.ANALYZING, error_code=PARSER_TIMEOUT, error_message=...)` — a same-status
  self-transition (REQ-003 Assumption A2), which increments `attempts` and records
  `last_error_code`/`last_error_message` (REQ-003 Assumption A5) without leaving `analyzing` — then
  raises `AnalyzerError(..., error_code=PARSER_TIMEOUT)`. TRANSIENT (AC5); the ledger row remains
  `analyzing`, satisfying AC5's literal wording ("records the failure without transitioning to a
  terminal state") and leaving the row re-enterable: a redelivered `analyze()` call re-enters at
  step 2 above as a further same-status self-transition, incrementing `attempts` again, so a retry
  coordinator (out of scope) can observe `attempts` to implement Contract #4's "poison after max
  dequeue" — which a terminal `failed` write would have made impossible (REQ-003's
  `_LEGAL_TRANSITIONS[Status.FAILED] = frozenset()`, `attempts` frozen at `failed`).
- F4. `DocumentIntelligenceParser.parse()` raises `HttpResponseError(status_code=429)` → mapped to
  `AnalyzerError(..., error_code=DOCINT_RATE_LIMITED)`; same self-transition pattern as F3
  (`ledger.transition(..., to_status=Status.ANALYZING, stage=Status.ANALYZING,
  error_code=DOCINT_RATE_LIMITED, error_message=...)`). TRANSIENT.
- F5. `DocumentIntelligenceParser.parse()` raises `HttpResponseError(status_code>=500)` → mapped to
  `AnalyzerError(..., error_code=DOCINT_UPSTREAM_ERROR)`; same self-transition pattern as F3.
  TRANSIENT.
- F6. Any parser raises an exception not mapped by F3–F5 (e.g. a `pymupdf` internal error, an
  unrecognized Azure SDK exception) → `Analyzer` catches it at the `parser.parse()` call site, logs
  the original exception, records it via the same self-transition pattern as F3
  (`error_code=PARSER_INTERNAL`), and re-raises `AnalyzerError(..., error_code=PARSER_INTERNAL)`.
  TRANSIENT.
- F7. Step 2's `ledger.transition(to_status=Status.ANALYZING, stage=Status.ANALYZING)` raises
  `LedgerTransitionInvalid` → per the step-2 note above, this can now only happen when the row is
  *not* already `analyzing` (a same-status redelivery succeeds without raising at all) — i.e. the
  row is unknown, or has progressed *past* `analyzing` (`chunking` or later) via a concurrent
  duplicate worker completing this REQ's own prior success. `Analyzer` triages via
  `is_benign_concurrent_loss(exc.observed_row, Status.ANALYZING, Status.ANALYZING)` (Assumption
  A2):
  - Benign (row already at/past `chunking` — a redelivered call after this REQ's own prior success):
    skip to step 3 directly, run `detect_type`/`parser.parse` as normal, but skip the step-6 ledger
    write (row is already past `analyzing`); return the `Element` list. No `AnalyzerError` raised —
    this is the Identity & Idempotency contract's "safely re-runnable" in action, not a failure.
  - Not benign (unknown `doc_id`, or row genuinely `failed` — which, after the F1–F6 fix in this
    revision, can only ever be reached via F1/F2's PERMANENT terminalization, never via a transient
    parser failure): re-raise `LedgerTransitionInvalid` unchanged — not this module's error code;
    propagates to the caller as a `RaggedError` per Contract #4, classified `PERMANENT` per
    REQ-004/REQ-003's own registration.
- F8. Step 6's `ledger.transition(to_status=Status.CHUNKING, stage=Status.CHUNKING)` raises
  `LedgerTransitionInvalid` (a concurrent duplicate worker already advanced this `doc_id` past
  `analyzing`) → same triage as F7, using `to_status=Status.CHUNKING` this time; benign → treat as
  success, return the `Element` list without erroring; not benign → re-raise unchanged. The row's
  `attempts` value at this point may already reflect one or more prior F3–F6 self-transition
  retries — that does not change this triage: `is_benign_concurrent_loss` reasons only about
  `status`, never `attempts`.

## 4. Contract compliance

- **Arrival Envelope**: `Analyzer.analyze` and `detect_type` accept only a validated `ArrivalEnvelope`
  (REQ-001) plus a `bytes_reader`; neither reads any source-specific field beyond the envelope's own
  attributes (`doc_id` for ledger keys, nothing else). Content-hash re-verification of the bytes is
  explicitly out of scope for this module (Assumption A4).
- **Identity & Idempotency**: every ledger write is keyed on `envelope.doc_id` (REQ-002's sha256
  identifier), never re-derived here. Re-running `analyze()` for the same `doc_id`/bytes is safe by
  construction: parsing itself is a pure read (no upsert), and every ledger write is made
  re-runnable either transparently (a same-status `analyzing → analyzing` self-transition, F3–F6, is
  itself a legal REQ-003 transition and never raises) or via the F7/F8 benign-concurrent-loss triage
  (Assumption A2) for the case where a concurrent worker has moved the row further along — never by
  any new idempotency mechanism of this module's own.
- **State Ledger**: this module owns exactly the `pending → analyzing`, `analyzing → analyzing`
  (same-status self-transition retries on TRANSIENT failure, F3–F6, REQ-003 Assumption A2), and
  `analyzing → {chunking | failed}` transitions (Contract #3's second stage), applied only through
  the existing `LedgerStore.transition()` (REQ-003) — no new transition table, no direct row
  mutation. Only the two PERMANENT paths (F1/F2) ever terminalize a row to `failed`; every TRANSIENT
  path (F3–F6) self-transitions and leaves the row re-enterable, per AC5's explicit requirement and
  REQ-003's own retry/`attempts` design — a terminal `failed` write on a TRANSIENT error would
  strand the `doc_id` (REQ-003's `_LEGAL_TRANSITIONS[Status.FAILED] = frozenset()`), making it
  impossible for `attempts` to ever reflect a genuine retry count.
- **Failure Taxonomy**: registers all six codes at import time via `register_error` (§1), following
  the exact `_DEFAULT_REGISTRY` pattern REQ-004 established; every parser exception is either mapped
  to one of the five specific codes (F1–F5) or wrapped as `PARSER_INTERNAL` (F6) — no Analyzer-raised
  exception is ever an unclassified bare exception (AC7: all six appear in `ErrorRegistry.all_codes()`
  after import). The module's ledger-write behavior on each raise is itself taxonomy-correct:
  PERMANENT (F1/F2) terminalizes the row per Contract #4's literal "mark failed, ack, never retry";
  TRANSIENT (F3–F6) self-transitions rather than terminalizing, so a coordinator's retry/redelivery
  can continue to advance `attempts` on the same row until a "poison after max dequeue" policy (out
  of scope here — coordinator/queue-layer territory, per REQ-003 §4's own deferral) decides to stop
  retrying.

## 5. Error codes

| Code | Classification | Trigger condition |
|---|---|---|
| `ANALYZER_UNSUPPORTED_TYPE` | PERMANENT | `detect_type` returns `UNSUPPORTED` — unknown magic bytes, encrypted PDF, corrupt file (F1); ledger row terminalizes to `failed` |
| `ANALYZER_NO_PARSER` | PERMANENT | `ParserRegistry.get(detected_type)` returns `None` for a supported type — registry misconfiguration (F2); ledger row terminalizes to `failed` |
| `PARSER_TIMEOUT` | TRANSIENT | `parser.parse()` exceeds `parser_timeout_seconds` (F3); ledger row self-transitions `analyzing → analyzing` |
| `DOCINT_RATE_LIMITED` | TRANSIENT | Azure Document Intelligence returns HTTP 429 (F4); ledger row self-transitions `analyzing → analyzing` |
| `DOCINT_UPSTREAM_ERROR` | TRANSIENT | Azure Document Intelligence returns HTTP 5xx (F5); ledger row self-transitions `analyzing → analyzing` |
| `PARSER_INTERNAL` | TRANSIENT | Any parser exception not mapped by the above — logs the original (F6); ledger row self-transitions `analyzing → analyzing` |

## 6. Config surface

New file `config/analyzer.yaml`:

```yaml
analyzer:
  scanned_pdf_probe_pages: 3            # operational tunable — code-authoritative, see below
  scanned_pdf_min_chars_per_page: 50    # operational tunable — code-authoritative, see below
  parser_timeout_seconds: 240           # operational tunable — code-authoritative, see below
  docint:
    endpoint_env_var: AZURE_DOCINT_ENDPOINT     # value read from env at process start, never here
    api_key_env_var: AZURE_DOCINT_API_KEY       # value read from env at process start, never here
    api_version: "2024-11-30"
    model_id: prebuilt-layout
  error_codes:
    ANALYZER_UNSUPPORTED_TYPE: PERMANENT
    ANALYZER_NO_PARSER: PERMANENT
    PARSER_TIMEOUT: TRANSIENT
    DOCINT_RATE_LIMITED: TRANSIENT
    DOCINT_UPSTREAM_ERROR: TRANSIENT
    PARSER_INTERNAL: TRANSIENT
```

`scanned_pdf_probe_pages`, `scanned_pdf_min_chars_per_page`, and `parser_timeout_seconds` are
ordinary operational tunables (Assumption A6) — the authoritative defaults live on
`AnalyzerConfig`'s dataclass fields (§1/§2); this YAML block is kept in sync via a dedicated test
(§7), following the exact precedent `config/ledger.yaml`'s `default_list_limit` established (no
config-loading mechanism exists anywhere in this codebase yet). `docint.endpoint_env_var` /
`api_key_env_var` name the environment variables the composition root (out of scope) must read to
construct `DocumentIntelligenceParser` — the actual endpoint URL and API key are never written to
this file or any code (house rules: secrets from env only). `error_codes` follows the same
documentation/audit-only convention as `config/errors.yaml`'s `known_codes` (§6 of REQ-004's LLD) —
not read at runtime; the registry populated via `register_error` at import time is authoritative.

## 7. Test plan

- `test_detect_type_digital_pdf_fixture` / `..._scanned_pdf_fixture` / `..._docx_fixture` /
  `..._image_fixture` — each returns the expected `DetectedType` from a real fixture file.
- `test_detect_type_never_uses_file_extension` — a fixture with a `.pdf` extension but non-PDF magic
  bytes returns `UNSUPPORTED`, not `DIGITAL_PDF` (AC3).
- `test_detect_type_encrypted_pdf_returns_unsupported`.
- `test_detect_type_corrupt_pdf_returns_unsupported`.
- `test_detect_type_empty_file_returns_unsupported`.
- `test_detect_type_pdf_below_char_threshold_classified_scanned` /
  `..._above_threshold_classified_digital` — parametrized over `scanned_pdf_min_chars_per_page`.
- `test_detect_type_never_raises` — every fixture above, malformed input included, returns a
  `DetectedType`, never an exception.
- `test_parser_registry_register_then_get_returns_parser`.
- `test_parser_registry_get_missing_returns_none` (feeds F2).
- `test_analyzer_analyze_digital_pdf_transitions_pending_to_analyzing_to_chunking` (AC1) — ledger
  row ends `chunking`; returns ≥ 1 element per page with visible text.
- `test_analyzer_analyze_scanned_pdf_with_recorded_docint_response_returns_table_element` (AC2) — no
  live API call; uses a recorded-response fake per story's Tests section.
- `test_analyzer_analyze_unsupported_type_raises_analyzer_error_permanent_ledger_ends_failed` (AC3)
  — asserts the ledger row's terminal `status == Status.FAILED`.
- `test_analyzer_analyze_no_registered_parser_raises_analyzer_error_permanent_ledger_ends_failed`
  (AC4) — asserts the ledger row's terminal `status == Status.FAILED`.
- `test_analyzer_analyze_parser_timeout_raises_parser_timeout_transient_ledger_self_transitions_analyzing`
  (AC5) — asserts the ledger row's `status` remains `Status.ANALYZING` (never `Status.FAILED`),
  `attempts` incremented by 1, and `last_error_code == PARSER_TIMEOUT` recorded — the literal
  behavior AC5 requires ("records the failure without transitioning to a terminal state").
- `test_analyzer_analyze_docint_429_raises_docint_rate_limited_transient_ledger_self_transitions_analyzing`
  — same self-transition assertions as the timeout test above, `last_error_code ==
  DOCINT_RATE_LIMITED`.
- `test_analyzer_analyze_docint_5xx_raises_docint_upstream_error_transient_ledger_self_transitions_analyzing`
  — same, `last_error_code == DOCINT_UPSTREAM_ERROR`.
- `test_analyzer_analyze_unmapped_parser_exception_wrapped_as_parser_internal_transient_ledger_self_transitions_analyzing`
  — asserts the original exception is logged, plus the same self-transition assertions as above,
  `last_error_code == PARSER_INTERNAL`.
- `test_analyzer_analyze_transient_failure_then_redelivery_increments_attempts_without_reaching_failed`
  — two consecutive `analyze()` calls for the same `doc_id`, each hitting `PARSER_TIMEOUT` (or any
  F3–F6 code), assert the ledger row's `attempts` increases by 1 on each call, `status` stays
  `Status.ANALYZING` throughout (never `Status.FAILED`), and the second call's step-2
  `ledger.transition` succeeds directly as a same-status self-transition rather than raising
  `LedgerTransitionInvalid` — the regression test for the F3–F6 fix (the row must remain
  re-enterable across transient retries).
- `test_analyzer_analyze_permanent_vs_transient_ledger_terminalization_contrast` — parametrized:
  F1/F2 (PERMANENT) end with `status == Status.FAILED`; F3–F6 (TRANSIENT) end with `status ==
  Status.ANALYZING` — asserts the two failure classes are never conflated in ledger-write behavior.
- `test_analyzer_analyze_swaps_in_fake_parser_with_no_analyzer_code_changes` (AC6) — registers a
  `FakeParser` against a `DetectedType`, asserts `Analyzer`/callers are unmodified.
- `test_analyzer_analyze_redelivered_after_prior_success_is_benign_noop` (Assumption A2, F7/F8) —
  pre-seed a ledger row already at `chunking`; re-invoke `analyze()`; asserts no exception, the
  `Element` list is returned, and the ledger row is untouched (still `chunking`, not re-written).
- `test_analyzer_analyze_redelivered_while_still_analyzing_is_plain_self_transition_not_f7` —
  pre-seed a ledger row already at `analyzing` (e.g. left there by a prior F3–F6 self-transition);
  re-invoke `analyze()`; asserts step 2's `ledger.transition` call succeeds directly with no
  `LedgerTransitionInvalid` raised (so `is_benign_concurrent_loss` is never even invoked) and
  `attempts` increments again — distinguishing this path from F7/F8's own triage, which only
  applies once the row has moved *past* `analyzing`.
- `test_analyzer_analyze_ledger_transition_invalid_non_benign_propagates_unchanged` — unknown
  `doc_id` (no pre-created row) → `LedgerTransitionInvalid` propagates, not swallowed.
- `test_analyzer_analyze_ledger_transition_invalid_non_benign_already_failed_propagates_unchanged` —
  pre-seed a ledger row already at `Status.FAILED` for a `doc_id` (e.g. via a prior F1/F2
  terminalization); invoke `Analyzer.analyze` for that same `doc_id`; asserts step 2's
  `ledger.transition(to_status=Status.ANALYZING, stage=Status.ANALYZING)` raises
  `LedgerTransitionInvalid`, that `is_benign_concurrent_loss(exc.observed_row, Status.ANALYZING,
  Status.ANALYZING)` classifies it non-benign (`observed_row.status == Status.FAILED` falls through
  every `True` branch of REQ-003's triage — §1/§3 F7 step 5 there), and that `Analyzer` re-raises
  `LedgerTransitionInvalid` unchanged rather than swallowing it or treating it as benign — the
  companion to the unknown-`doc_id` test above, covering the distinct "row genuinely already
  `failed`" branch F7's own prose in this LLD calls out.
- `test_pymupdf_parser_returns_elements_in_reading_order_on_fixture` — non-empty PDF never returns
  an empty element list.
- `test_docint_parser_returns_table_element_with_row_column_metadata` — recorded-response fake.
- `test_docint_parser_maps_429_and_5xx_to_declared_codes` — recorded-response fakes for each status.
- `test_all_six_analyzer_codes_registered_after_import` — `ErrorRegistry.all_codes()` contains all
  six with the declared classifications (AC7).
- `test_config_analyzer_yaml_tunables_match_analyzer_config_defaults` — sync-via-test guard for §6
  (Assumption A6), same pattern as REQ-003's `default_list_limit` test.
- Integration, opt-in only (`RUN_LIVE_DOCINT_TESTS=1`, skipped in CI by default): one real DocIntel
  call on a scanned fixture — requires credentials, per story.

## 8. Budget

- p95 latency added per document (PyMuPDF, 20-page digital PDF): < 3 seconds locally (story budget;
  dominated by local `pymupdf` extraction, no network call).
- p95 latency added per document (DocIntel, 10-page scanned PDF): < 60 seconds end-to-end (story
  budget; dominated by the upstream Azure service, not this module's own logic).
- Cost per document: $0 for `PyMuPDFParser` (fully local); ~$0.01–0.03 per page for
  `DocumentIntelligenceParser` (billed upstream by Azure, not an LLM-token cost).
- Memory: streams pages where the underlying parser supports it; no full-document in-memory
  materialization above 50 MB (story budget) — `Element` lists are the only long-lived in-process
  structure this module retains.
