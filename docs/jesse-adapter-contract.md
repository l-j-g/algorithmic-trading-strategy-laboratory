# Jesse adapter contract

The adapter boundary uses two versioned JSON documents:

- `jesse-execution-request.schema.json`: strategy, operation, routes and date
  windows, parameters, and evaluation gates.
- `jesse-execution-result.schema.json`: session ID, dashboard URL, normalized
metrics, terminal status, and structured error evidence.

## Session configuration snapshot

Every backtest, significance, and Monte Carlo draft receives an explicit
session exchange snapshot:

```json
{
  "balance": 10000,
  "fee": 0.0005,
  "futures_leverage": 1,
  "futures_leverage_mode": "cross"
}
```

Jesse stores these values on the draft and snapshots them when `run_*` starts.
The runner must not reread mutable global exchange configuration while the
session is running. ATS evidence keeps configured leverage separate from
observed effective leverage:

- `configured_futures_leverage`
- `leverage_mode`
- `effective_leverage_mean`, `effective_leverage_p95`, `effective_leverage_max`
- `liquidation_count`

Compact session polling returns status, metrics, and liquidation count without
the trade/order collection. Full trades remain a diagnostic-only path.

Python producers and consumers use `JesseExecutionRequest` and
`JesseExecutionResult` from `ats_lab.jesse_contracts`.

## Safety boundary

`transport` is fixed to `jesse_mcp`. The contract module only parses and
validates data. It does not import Jesse, read strategy/config/candle files,
launch subprocesses, or perform trading operations. A worker implementing this
contract must send every Jesse strategy and trading action through Jesse MCP.

Laboratory state changes occur outside adapter. Supervisor claims one batch,
executor returns Jesse evidence only, and laboratory persists runs. Separate
isolated analyzer receives compact evidence and returns exact evaluation
coverage. Laboratory then persists verdicts and finishes work. Analyzer failure
does not repeat durable backtests.

## Direct mechanical executor

`ats_lab.direct_mcp_executor` owns ordinary backtest draft creation, start,
bounded polling, terminal fetch, and compact result construction. It uses the
configured `http://127.0.0.1:9002/mcp` endpoint and supported Streamable HTTP
session semantics. Session IDs and exact terminal metrics are checkpointed in
SQLite. Restart resumes polling and never invokes a model for terminal polling.
Harness-fix recovery must remove only a matching stopped checkpoint after
proving no finished run exists. Recovery records session ID and invalidation
reason in immutable work-item event history, then permits one replacement
session. Any finished run prevents checkpoint invalidation and requeue, avoiding
duplicate valid execution.

New or materially changed strategy source gets one separate bounded
`prepare_strategies` Agent turn. That turn must use Jesse MCP and return one
bounded `strategy_readiness` entry per work item (`ready`, `missing`, or
`invalid`) plus IDs for entries marked ready. `ready` entries also carry four
bounded contract receipts: positive quantity/95% available-margin sizing,
Jesse-shaped stop-loss/take-profit sequences, indicator signatures, and
strategy callback signatures. The ATS-side validator rejects missing or failed
receipts before any backtest session is created. Direct execution proceeds only
for discoverable, loadable strategies with all receipts passing. Missing or
invalid classes and contract failures become terminal strategy evidence for
analysis; source and patches are forbidden from ATS payloads.
Unsupported operations remain on the existing Agent path. Set
`jesse_executor.enabled = false` for full fallback.

## Sizing contract

Strategy sizing is bounded by the session, not tuned as a research parameter.
The maximum entry notional is:

```text
0.95 * available_margin * session_leverage
```

When a strategy declares fixed `L_max`, `session_leverage` must be no greater
than `L_max`. `L_max` is a contract ceiling and must not be added to HPO
parameters or searched as an optimization dimension. Use available margin,
never starting balance, for this cap.
