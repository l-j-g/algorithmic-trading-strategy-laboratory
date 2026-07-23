# Jesse adapter contract

The adapter boundary uses two versioned JSON documents:

- `jesse-execution-request.schema.json`: strategy, operation, routes and date
  windows, parameters, and evaluation gates.
- `jesse-execution-result.schema.json`: session ID, dashboard URL, normalized
  metrics, terminal status, and structured error evidence.

Python producers and consumers use `JesseExecutionRequest` and
`JesseExecutionResult` from `ats_lab.jesse_contracts`.

## Safety boundary

`transport` is fixed to `jesse_mcp`. The contract module only parses and
validates data. It does not import Jesse, read strategy/config/candle files,
launch subprocesses, or perform trading operations. A worker implementing this
contract must send every Jesse strategy and trading action through Jesse MCP.

Laboratory state changes occur outside the adapter: claim work before building
a request, then return Jesse evidence to Agent. Agent adds a research
evaluation in the same response so the worker can persist run and verdict
atomically before transitioning the work item. Execution completion and research
quality remain separate facts even though they share one response.
