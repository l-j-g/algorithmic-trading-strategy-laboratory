# Token-efficiency engineering backlog

Engineering backlog only. Never import these entries into ATS research
`work_items`, synthesis cohorts, or strategy queue state.

## 1. Deterministic Jesse session runner — implemented

Expected impact: remove repeated 1.2M-2.1M cache-read-token model sessions from
long-running execution polling; likely largest remaining CLI-worker reduction.

Acceptance criteria:

- Use a documented Jesse adapter/API for start, status, terminal metrics, and
  cancellation. Do not infer endpoints from UI traffic.
- One model turn may prepare bounded execution specification. Script owns
  start/poll/backoff/terminal detection/persistence after that point.
- Poll emits nothing until terminal except bounded operator telemetry.
- Restart resumes using persisted session ID without replaying complete session
  payload into any model.
- Route evidence, partial-batch retry, exact raw metrics, and sizing invariants
  remain covered by regression tests.

Implemented by `ats_lab.direct_mcp_executor` using Jesse MCP Streamable HTTP:
initialize, `mcp-session-id`, `tools/call`, SSE/JSON decode, durable draft
checkpoint, start, bounded exponential polling, terminal compact metrics, and
privacy-safe telemetry. `.ats-lab/config.toml` enables it; disabling
`jesse_executor.enabled` restores complete Agent execution fallback.

Fake-server parity fixture, one two-route run with three polls:

- before: one Agent model turn, 3,153-byte model request prompt, with polling
  performed inside that turn;
- after: zero model turns, seven HTTP JSON-RPC calls, 1,455 request bytes and
  2,677 response bytes total;
- terminal polling telemetry: `model_call_count=0`, `poll_count=3`;
- terminal `metrics` and compact `raw_result.metrics` remain exactly equal.

## 2. Purpose-built minimal ats-lab worker profile

Expected impact: remove most remaining 11,277-byte skill index and unrelated
35,327-byte system/context prompt from every ATS worker call.

Acceptance criteria:

- Dedicated profile contains only ATS/Jesse execution rules needed by selected
  task.
- Execution profile exposes Jesse MCP only; analysis and synthesis expose zero
  tools.
- `executor prompt-size --json` snapshot test enforces fixed byte ceilings.
- Profile lifecycle/config ownership documented outside this repository before
  launcher makes it mandatory.

## 3. Split execution submission from terminal collection — implemented

Expected impact: reduce model-call count toward one preparation call per batch,
then zero calls per poll.

Acceptance criteria:

- Durable database state distinguishes submitted session from terminal run.
- Supervisor can recover abandoned submitted sessions deterministically.
- Partial batch completion persists terminal members without replaying them.
- Bounded retry and explicit terminal disposition cover missing/expired
  sessions.
- No complete Jesse session response stored in prompt-bearing fields.

Durable `direct_execution_sessions` rows checkpoint session IDs before start.
Restart polls saved sessions without draft creation or rerun. Finished members
remain reusable while failed/timed-out members retain explicit retry blockers.
Durable `direct_strategy_preparations` prevents repeating bounded preparation
turns.

## 4. Telemetry rollup and budget alarms

Expected impact: indirect; detects regressions before cached traffic returns to
current scale.

Acceptance criteria:

- Bounded daily rollup by task type: request/response bytes, calls, input,
  output, cache-read tokens, p50/p95.
- Alert thresholds for request bytes and calls per completed work item.
- Rotation/retention bounded by size and age.
- No prompts, responses, credentials, session IDs, metrics, or strategy source.

Current implementation supplies privacy-safe Agent JSONL records plus SQLite
direct-executor records. Remaining: bounded daily p50/p95 rollup, alarms, and
rotation/retention.

## 5. Deterministic verdict expansion

Expected impact: eliminate analysis model calls where existing gates fully
determine verdict and next action.

Acceptance criteria:

- Explicit rule table covers only unambiguous cases.
- Model remains for conflicting evidence and research interpretation.
- Golden tests preserve route splits, HPO stability evidence, and promotion
  safety.
- Telemetry compares calls avoided against unchanged dispositions.
