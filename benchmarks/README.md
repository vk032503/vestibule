# Parser adapter benchmarks

This folder is a market-tracking commitment, not a running benchmark harness (REQ-005
LLD Assumption A8 / the story's "Market-awareness note"). It defines no class,
function, error code, ledger state, or contract surface, and no code in the
`ingestion.analyzer` package depends on this folder existing.

## What a future benchmark run would measure

When a future PR adds one, a parser-adapter benchmark run is expected to measure, per
candidate adapter, against a labeled fixture set:

- **Extraction accuracy** — element-level precision/recall against hand-labeled ground
  truth (headings, paragraphs, tables with row/column structure, captions), separately
  for born-digital and scanned/OCR'd documents.
- **Latency** — p50/p95 wall-clock time per document, per page, at representative page
  counts (this REQ's own budget: PyMuPDF < 3s p95 for a 20-page digital PDF locally;
  Document Intelligence < 60s p95 end-to-end for a 10-page scanned PDF).
- **Cost per page** — $0 for local/CPU-bound adapters (e.g. PyMuPDF); metered per-page
  or per-call cost for managed OCR/layout/VLM services (e.g. Azure Document
  Intelligence's ~$0.01-0.03/page).

## How a candidate adapter is registered for a benchmark run

The `ParserAdapter`/`ParserRegistry` design (`ingestion/analyzer/registry.py`)
deliberately makes this a registration-only exercise, never a change to `Analyzer`
itself — the same swap-in mechanism this REQ's acceptance criteria (AC6) already
requires and tests:

```python
from ingestion.analyzer.model import DetectedType
from ingestion.analyzer.registry import ParserRegistry

registry = ParserRegistry()
registry.register(DetectedType.SCANNED_PDF, MyCandidateAdapter(...))
```

Any candidate — a future Docling adapter, an Unstructured adapter, a VLM-based parser,
or a newer entrant — need only implement `ParserAdapter.parse(envelope, bytes_reader)
-> list[Element]` (`ingestion/analyzer/registry.py`) and register itself against the
`DetectedType`(s) it targets. `Analyzer` dispatches purely by looking up the registered
adapter; it never imports or references any specific adapter implementation.

## What this REQ does *not* build

No benchmark-running automation, scoring harness, labeled fixture set, or CI job is
built by REQ-005. This folder and this README are scaffolding and a market-tracking
commitment only, per the story's own framing — a future REQ, scoped and reviewed on its
own, would add the actual harness.
