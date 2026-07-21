# Ingestion Framework — Agent-Team Starter Kit (Pro-plan tuned)

Seven-role pipeline trimmed to four agents + you, built for Claude Code on a Pro plan.
You are the Story author and the two human gates (design approval, merge).

## The lifecycle (tracked entirely in GitHub)

```
GitHub Issue (label: stage:story)
  → you write docs/stories/REQ-<id>.md from TEMPLATE.md        [label → stage:design]
  → @lld produces docs/designs/REQ-<id>-lld.md                 
  → @design-reviewer verdict on the LLD                        
  → YOU approve (commit design, flip label)                    [label → stage:dev]
  → @developer implements on branch req/<id>, opens PR
  → @reviewer verdict on the PR diff                           [label → stage:review]
  → YOU merge                                                  [label → done]
```

Branch protection: require the reviewer verdict before merge. Every requirement's full
history = its issue thread + committed story/design + PR review comments.

## Running it (inside Claude Code, one component per session)

```
# fresh session per component — kill context between components
claude
> Use the lld subagent on docs/stories/REQ-001.md
> Use the design-reviewer subagent on docs/designs/REQ-001-lld.md
# fix findings, approve, then (new session):
> Use the developer subagent to implement docs/designs/REQ-001-lld.md
> Use the reviewer subagent on the current diff
```

## Pro-plan discipline (why this kit is shaped this way)
- One component per session; fresh session per component (context = money).
- Plan mode before implementation; Sonnet for dev/review, Opus only for Phase 1 designs.
- Reviews read diffs, not repos (enforced in reviewer.md).
- CLAUDE.md stays under 40 lines; procedure detail lives in role files (lazy-loaded).
- Batch agent work into 1–2 focused blocks/day; watch Settings → Usage weekly.
- Overflow week? Enable usage credits temporarily instead of upgrading plans.

## Phase 1 build order (full pipeline: lld → design-review → dev → review)
1. REQ-001 Arrival Envelope (models + validation)
2. REQ-002 Identity & Idempotency (id derivation + upsert helpers)
3. REQ-003 State Ledger (state machine + Table Storage adapter)
4. REQ-004 Failure Taxonomy (classifier + retry/poison policy engine)

Phases 2+ use the light path: you + @developer + @reviewer (skip lld/design gate
for low-risk components; the contracts in CLAUDE.md still bind everything).

## GitHub labels to create
`stage:story` `stage:design` `stage:dev` `stage:review` `done` `blocked`
