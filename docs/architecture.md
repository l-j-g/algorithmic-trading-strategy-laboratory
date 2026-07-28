# Architecture

Algorithmic Trading Strategy Laboratory separates concerns that are commonly
mixed together in research scripts.

```text
Scheduler or agent
       |
       v
Typed JSON contracts --> transactional work queue
                              |
                              v
                      execution adapter
                              |
                              v
                        run evidence
                              |
                              v
                         evaluation
                              |
                              v
                    candidates and synthesis
```

See [job synthesis](synthesis.md) for entry-rule lineage and significance gates.

## Ownership

- Laboratory owns specifications, queue state, evidence, evaluations and views.
- Execution adapters own communication with a backtesting framework.
- An external scheduler or agent decides when to invoke short CLI commands.
- The continuous worker claims work and calls an external JSON adapter; it does
  not contain backtester, Agent, Memory, or Jesse operations.
- Memory systems may store durable preferences and conclusions, never locks or
  authoritative experiment results.
- Markdown and HTML are output formats, not operational databases.

## State machines

Work state:

```text
scheduled -> ready -> running -> finished
                         |
                         +-> waiting_retry -> ready
                         +-> blocked
```

Research verdict:

```text
reject | revise | inconclusive | hpo_candidate | paper_trade_candidate
```

Execution completion and research quality are intentionally independent.

## Storage

SQLite supplies transactions, foreign keys, idempotent imports and queryable
history without requiring a database service. Versioned JSON Schemas define
agent and adapter boundaries. Python dataclasses provide in-process types.

The canonical local database is `.ats-lab/laboratory.sqlite3` in this
repository. It stores experiment specifications, work items, runs, evaluations,
artifacts, events, and synthesis cohorts. It does not store Jesse candle data or
strategy source. See [Jesse workspace integration](workspace-integration.md).

## Agent integration

The CLI emits JSON and performs one bounded operation per invocation. It can be
called by Agent, Codex, cron, CI or another orchestrator. No agent-specific
memory format is required.

The optional [Agent and Memory launcher](executor-memory-launcher.md) provides a
bounded subprocess adapter while preserving these ownership boundaries.

[Resource policy](resource-policy.md) keeps agent calls sparse and delegates
large RST, HPO and Monte Carlo batches to local Jesse compute.
