# Agent and Memory launcher

Laboratory supervisor dispatches execution and analysis batches to separate
Agent turns. Memory remains Agent' optional memory provider.
Laboratory SQLite remains authoritative for queue state and evidence.

```text
ATS supervisor -> executor Agent turn -> Jesse MCP batch
              -> analyzer Agent turn -> evaluations + optional 25-chain cohort
```

## Local configuration

Create `.ats-lab/config.toml`. This directory is ignored by Git.

```toml
[repositories]
jesse = "/absolute/path/to/jesse-workspace"

[executor]
executable = "executor"
profile = "ats-lab"
timeout_seconds = 3600
# Fallback execution/HPO calls are bounded separately.
execution_timeout_seconds = 900
# Strategy preparation is bounded separately so one Jesse inspection cannot
# hold the supervisor for the full general-purpose Agent timeout.
preparation_timeout_seconds = 300
# model = "provider/model"
# provider = "provider"
# Defaults shown explicitly:
execution_toolsets = ["jesse"]
analysis_toolsets = ["context_engine"]   # built-in zero-tool set
synthesis_toolsets = ["context_engine"]  # built-in zero-tool set
# telemetry_path = "agent-transport.jsonl"

[jesse_executor]
enabled = true
mcp_url = "http://127.0.0.1:9002/mcp"
timeout_seconds = 60
poll_initial_seconds = 2
poll_max_seconds = 5
max_polls = 3
dashboard_api_base_url = "http://127.0.0.1:9000"
dashboard_display_base_url = "http://127.0.0.1:9000/#/backtest"
```

Run stack preflight before supervisor:

```bash
PYTHONPATH=src python3 -m ats_lab.cli preflight
```

Checks order: Docker daemon; Jesse PostgreSQL container running; `pg_isready`;
read-only `SELECT 1`; expected public tables `candle`, `backtestsession`, and
`significancetestsession`; Jesse dashboard; Jesse MCP protocol; Memory health
API. Supervisor uses same fail-closed gate before any claim/model dispatch.
Infrastructure failure creates precise `infrastructure_preflight_failed`
result and consumes zero strategy attempts. macOS supported through Docker
Desktop CLI (`docker info`). PostgreSQL checks use argument-vector `docker exec`
inside container with non-secret identity defaults `postgres`, `jesse_user`, and
`jesse_db`. Override them with `ATS_LAB_JESSE_POSTGRES_CONTAINER`,
`ATS_LAB_JESSE_POSTGRES_USER`, and `ATS_LAB_JESSE_POSTGRES_DATABASE`.
No password, credential row, or `exchangeapikeys` content is read or logged.
Optional `ATS_LAB_MEMORY_HEALTH_URL` changes local health endpoint without
storing credentials.

Dashboard fallback authentication reads only `JESSE_AUTH_TOKEN` or
`JESSE_DASHBOARD_PASSWORD` from supervisor process environment. Never place
either secret in TOML, logs, dispatch payloads, or SQLite.

Memory configuration stays in Agent. Do not place Memory credentials in this
file or commit them.

Direct executor handles ordinary homogeneous-window backtests. Source
preparation, optimizer parameters, fee/config mutation, heterogeneous windows,
and non-backtest operations retain Agent fallback. Set
`jesse_executor.enabled = false` for complete legacy execution fallback.
Endpoint and dashboard URL contain no credentials. Dashboard auth remains in
approved local environment handling and is never logged.

## Run

From the laboratory repository:

```bash
ats-lab supervisor --continuous
```

The supervisor auto-selects this launcher when `.ats-lab/config.toml` exists.
Use `--dispatch-command` or `ATS_LAB_DISPATCH_COMMAND` to override it.

Launcher reads one laboratory request from standard input. It runs one bounded
Agent `--oneshot` process with an argument vector, never a shell. Executor turn
returns one run result per claimed work item and cannot evaluate or synthesize.
Executor argv exposes only configured Jesse MCP server by default. Analyzer and
synthesis argv use Agent' zero-tool `context_engine` set. Launcher rejects
requests containing trades, charts, logs, complete session payloads, or private
strategy source before starting Agent.
Analyzer turn receives compact serialized `NormalizedEvidence` records only,
and returns verdict/finding/next-action patches. Separate synthesis turns run
only after queue reaches low watermark. Analyzer receives no raw Jesse metrics,
dashboard-specific payload, HPO-specific metric schema or prose
`metrics_summary`. HPO validation parameters appear only in executor
`execution_context`; they never replace strategy defaults.

Launcher does not call Jesse, Memory, or SQLite directly. Agent accesses Jesse
through its configured Jesse MCP tools. Memory may inform reasoning, but cannot
claim jobs, change queue state, or become run evidence.

## Failure behavior

- Missing/invalid local config: blocked as `launcher_configuration`.
- Agent unavailable or exits unsuccessfully: retry.
- Timeout: retry as `executor_timeout`.
- Invalid agent response: retry as `invalid_executor_result`.
- Request larger than 1 MB: blocked as `request_too_large`.
- Terminal strategy/harness execution: durable failed-run evidence, then
  analysis decides `revise` or `reject`.
- Active Jesse session: transient defer without charging strategy attempts.

Supervisor owns final queue transitions for every result. Durable executions
awaiting analysis remain resumable after analyzer failure.

Synthesis requests use the same launcher but return exactly 25 typed research
chains in one response. SQLite supplies compact improvement candidates and
concept learnings using the same normalized evidence serializer; Memory memory
never replaces authoritative feedback.

## Transport telemetry

Each configured launcher call passes Agent a private temporary `--usage-file`.
Launcher deletes that file after reading it and appends one redacted record to
`.ats-lab/agent-transport.jsonl` by default. Records contain task type, request
bytes, response bytes, model-call count, and available input/output/cache-read
token totals. They never contain prompt, response, session ID, credentials,
strategy source, or metrics.

Current fresh ats-lab CLI measurement before task scoping:

- 63,775 tool-schema bytes across 33 tools.
- 11,277 skill-index bytes.
- 35,327 system-prompt bytes.

`analyze_batch`, `analyze_hpo`, and `synthesize_batch` now select
`context_engine`, measured as zero tools and 2 JSON bytes (`[]`). Fixed
tool-schema transport therefore drops 63,773 bytes per such model call before
provider framing. Executor savings depend on Jesse MCP schema size; unrelated
built-in tool schemas are excluded.
