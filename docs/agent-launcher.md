# Agent and Memory launcher

Laboratory worker can dispatch each claimed item to Agent. Agent performs
agent reasoning and tool use. Memory remains Agent' optional memory provider.
Laboratory SQLite remains authoritative for queue state and evidence.

```text
Laboratory worker -> Agent launcher -> Agent -> Jesse MCP
                         |
                         +-> Memory context (non-authoritative)
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
# model = "provider/model"
# provider = "provider"
# toolsets = ["terminal", "file"] # optional built-in toolset override; MCP is inherited from profile
```

Memory configuration stays in Agent. Do not place Memory credentials in this
file or commit them.

## Run

From the laboratory repository:

```bash
ats-lab worker --continuous
```

The worker auto-selects this launcher when `.ats-lab/config.toml` exists.
Use `--dispatch-command` or `ATS_LAB_DISPATCH_COMMAND` to override it.

Launcher reads one laboratory request from standard input. It runs one bounded
Agent `--oneshot` process with an argument vector, never a shell. Agent must
return one JSON result with outcome `finished`, `blocked`, or `retry`. Finished
research work includes both `evidence.run` and `evidence.evaluation`; worker
stores them atomically.

Launcher does not call Jesse, Memory, or SQLite directly. Agent accesses Jesse
through its configured Jesse MCP tools. Memory may inform reasoning, but cannot
claim jobs, change queue state, or become run evidence.

## Failure behavior

- Missing/invalid local config: blocked as `launcher_configuration`.
- Agent unavailable or exits unsuccessfully: retry.
- Timeout: retry as `executor_timeout`.
- Invalid agent response: retry as `invalid_executor_result`.
- Request larger than 1 MB: blocked as `request_too_large`.

Worker owns final queue transitions for every result.

Synthesis requests use the same launcher but return exactly 25 typed research
chains in one response. SQLite supplies compact improvement candidates and
concept learnings; Memory memory never replaces authoritative feedback.
