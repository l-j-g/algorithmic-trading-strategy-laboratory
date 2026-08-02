# Memory advisory research memory

ATS SQLite remains canonical for queue state, runs, normalized evidence,
evaluations, dependencies, HPO state, and synthesis cohorts. Memory stores only
compact evidence-derived advisory learnings. Memory cannot change ATS state or
bypass deterministic gates.

## Supported Memory v3 operations

Adapter uses documented public API only:

- Health: `GET /health`.
- Namespace get-or-create: `POST /v3/workspaces`, then workspace-scoped
  `POST .../peers` and `POST .../sessions`.
- Strategy-learning write: `POST .../sessions/{session_id}/messages`.
- Semantic recall and delivery deduplication: documented hybrid search via
  `POST .../sessions/{session_id}/search`.

Dedicated namespace:

- workspace: `ats-lab-memory`
- peer: `ats-lab-memory-peer`
- session: `strategy-learnings-v1`
- message metadata kind: `strategy_learning`

This peer/session is machine research memory. It is not a human profile.
Operational incidents and synthesis decisions require separate future sessions;
this implementation writes strategy learnings only.

## Safety and durability

Validated evaluation and `research_memory_outbox` insertion share one SQLite
transaction. Dispatcher runs later. Memory outage leaves evaluation durable and
outbox retryable. Errors retain bounded codes only. Infrastructure retries do
not alter strategy attempts.

Payload allowlist excludes Jesse session IDs, dashboard URLs, credentials,
strategy source, raw responses, trades, charts, logs, and tracebacks. Text and
payload sizes are bounded. Numbers must be finite. Missing normalized metrics
remain absent.

Recall is bounded by item, text, and byte limits. Records enter synthesis only
under `advisory_memory` with `trust=untrusted_advisory_data`. Malformed or
unavailable memory sets `memory_degraded=true`; synthesis remains SQLite-only.
Historical verdicts remain observations, never readiness or promotion inputs.

## Operator commands

```bash
ats-lab memory-status
ats-lab memory-sync --dry-run --limit 25
ats-lab memory-sync --apply --limit 25
```

`ATS_LAB_MEMORY_API_KEY` is read from process environment only when required. Never put
it in tracked configuration, CLI output, SQLite, or logs. Local base URL may be
set with `ATS_LAB_MEMORY_URL`.
