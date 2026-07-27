# Vestibule

The entry hall of your RAG pipeline. Every document passes through, gets identified, checked, and logged — before anything downstream sees it.

Python 3.11+ · MIT · [v0.1.0](../../releases/tag/v0.1.0)

---

Every team building enterprise RAG rebuilds the same plumbing. Deterministic document IDs so retries don't duplicate. A ledger to answer "did this document make it in?" A failure taxonomy so a corrupt PDF doesn't get retried five times before landing in a poison queue. Governance so an intern's query can't return the CEO's documents.

None of the good parsing libraries ship this. None of the orchestration frameworks either. Everyone builds it — badly — the first time they hit production.

Vestibule is the plumbing. It has strong opinions about a small number of things and no opinions at all about the things you should be free to choose.

## What it does today (v0.1)

The four contracts every production RAG pipeline needs, as a Python package you can install and build on.

**Arrival envelope.** One normalized shape every document enters through, no matter the source. Strict schema, deterministic identity, default-deny ACLs. Adapters upstream of the envelope can be anything; everything downstream depends on this contract and nothing else.

**Deterministic identity.** `doc_id` and `chunk_id` are pure functions of their inputs. Same document, same ID, forever. This is what makes at-least-once queue delivery safe — retries overwrite instead of duplicating.

**State ledger.** One row per document, one legal state machine (`pending → analyzing → chunking → embedding → indexing → indexed`). Illegal transitions raise. Thread-safe in-memory implementation ships now; Azure Table Storage and Cosmos DB adapters ship in v0.2.

**Failure taxonomy.** Every error the framework raises is registered with a `PERMANENT` or `TRANSIENT` classification. Callers decide their retry policy; the classification decides whether retry is even the right move. Unclassified errors default to `TRANSIENT` and log a warning.

## What it does not do yet

- No parsers, chunkers, embedders, or indexers. Those ship in v0.2.
- No retry engine. Callers use `tenacity` (or their own) with our `classify()`.
- No admin UI, no evaluation loop, no cross-vendor parser arbitrage. Those are the roadmap.

If you need those parts today, Vestibule is the wrong entry point. If you're planning to build them and don't want to rewrite identity and state handling six months in, this is where you start.

## Install

```bash
pip install vestibule
```

Requires Python 3.11.

## Five-minute example

```python
from vestibule import (
    DocumentEnvelope,
    InMemoryLedgerStore,
    Status,
    classify,
    Severity,
)

# An adapter constructs an envelope from the arriving document.
env = DocumentEnvelope(
    source="blob:hr-uploads",
    blob_path="policies/leave-2026.pdf",
    allowed_groups=["hr-staff", "all-employees"],
    trust_tier="internal_verified",
    content_hash="sha256:e7b1...9f04",
)

# The ledger keys everything on doc_id. Idempotent — retries hit the same row.
ledger = InMemoryLedgerStore()
ledger.create(env.doc_id, envelope_summary=env.summary())

# Every stage transition is a single call. Illegal transitions raise.
ledger.transition(env.doc_id, to_status=Status.ANALYZING, stage="analyzing")
ledger.transition(env.doc_id, to_status=Status.CHUNKING, stage="chunking")
# ...
ledger.transition(env.doc_id, to_status=Status.INDEXED, stage="indexing")

# When something fails, classification decides what happens next.
try:
    do_embedding(env)
except SomeExternalTimeout:
    match