# Jesse workspace integration

## Repository roles

The laboratory repository owns the research harness and durable operational
state. The configured Jesse repository owns strategies, Jesse configuration,
and candle/market data.

| Concern | Canonical owner |
|---|---|
| Queue and leases | `.ats-lab/laboratory.sqlite3` |
| Experiment specifications | `.ats-lab/laboratory.sqlite3` |
| Run metrics and session references | `.ats-lab/laboratory.sqlite3` |
| Evaluations and synthesis cohorts | `.ats-lab/laboratory.sqlite3` |
| Harness code and JSON contracts | `src/ats_lab/` |
| Worker/resource configuration | `.ats-lab/config.toml` |
| Strategy source | configured Jesse repository |
| Candles and market data | Jesse, accessed through Jesse MCP |
| Legacy readable research notes | `jesse-src/research/` |

The laboratory does not copy Jesse candles into SQLite. “Training data” in this
workflow means the historical market data selected in each experiment route;
Jesse owns and serves that data. ATS Lab stores the experiment definition and
normalized evidence produced from it.

## Local binding

The ignored `.ats-lab/config.toml` contains:

```toml
[repositories]
jesse = "<repo-root>/src/repos/jesse-src"
```

Agent uses that workspace and performs all strategy, candle, configuration,
and backtest operations through Jesse MCP. The ATS Lab worker owns claims and
persists the returned run plus evaluation in one transaction.

On the current workstation, `jesse-src/.ats-lab` points to this repository's
`.ats-lab` directory for read-compatible local access. This pointer is machine
configuration, not portable committed state.

From `jesse-src`, use:

```bash
research/automation/ats_lab.sh queue
research/automation/ats_lab.sh candidates
research/automation/ats_lab.sh audit
```

`ATS_LAB_REPOSITORY` and `ATS_LAB_DATABASE` override the defaults.

## Safety and recovery

- Never initialize a second database from the Jesse workspace.
- Never commit `.ats-lab/config.toml`, SQLite files, WAL/SHM files, credentials,
  or candle data.
- Never edit SQLite manually to change lifecycle state.
- Use `ats-lab reconcile`, `ats-lab normalize-blockers`, and `ats-lab audit`.
- Keep one database writer boundary: the ATS Lab worker/CLI.
- Treat Markdown imports as migration input, not a competing operational queue.
