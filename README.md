# Algorithmic Trading Strategy Laboratory

Evidence-first infrastructure for designing, scheduling, executing and
evaluating algorithmic trading strategy research.

The laboratory keeps four concerns separate:

1. **Future work** — experiments waiting to run.
2. **Execution evidence** — immutable sessions, routes, metrics and errors.
3. **Evaluation** — explicit gates and research verdicts.
4. **Synthesis** — searchable history and candidate comparison.

It does not place live orders, manage exchange credentials or promise profitable
strategies.

## Why this exists

Research workflows often begin as Markdown queues and shell scripts. Over time,
execution state, results, agent prompts and historical notes become entangled.
Rejected work remains in the “queue,” failures are confused with completed
experiments, and agents repeat old research because evidence is hard to query.

This project replaces that sprawl with:

- typed Python domain contracts;
- versioned JSON Schemas for agent and adapter inputs;
- transactional SQLite persistence;
- separate work state and research verdicts;
- idempotent migration from legacy Markdown/JSON ledgers;
- short JSON-producing commands suitable for agents, cron and CI;
- queryable candidate and active-queue views.

## Status

Early alpha. Core schema, contracts, queue transitions, legacy importer, audit
tools and candidate views are implemented. Execution-framework adapters and
ranking extensions are the next stage.

## Architecture

```text
Agent / Codex / cron / CI
             |
             v
        ats-lab CLI
             |
             v
   typed contracts + SQLite
      |         |         |
    queue     evidence  evaluation
      |                   |
      +---- execution ----+
             adapter
```

The core is agent-agnostic and backtester-agnostic. A Jesse adapter is the first
planned execution integration. Agent and Codex can orchestrate the same short,
idempotent commands. Memory may supply cross-session memory, but SQLite remains
the authoritative source for queue and evidence state.

See [architecture](docs/architecture.md) for boundaries and state machines.
See [Jesse workspace integration](docs/workspace-integration.md) for the exact
harness, database, repository, and market-data ownership boundary.

## Requirements

- Python 3.11+
- No runtime dependencies

## Installation

```bash
git clone https://github.com/l-j-g/algorithmic-trading-strategy-laboratory.git
cd algorithmic-trading-strategy-laboratory
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
```

## Quick start

Initialize a laboratory database:

```bash
ats-lab init
```

Inspect future work and candidates:

```bash
ats-lab queue
ats-lab reconcile
ats-lab candidates
ats-lab candidates --verdict hpo-candidate
```

Claim one ready item transactionally:

```bash
ats-lab claim --worker research-agent-1
```

Every command emits JSON. A second worker cannot claim the same ready item.

### Run a worker

Connect any agent runner through a small JSON stdin/stdout adapter:

```bash
ats-lab worker --dispatch-command './bin/dispatch-agent' --continuous
```

When `.ats-lab/config.toml` exists, omit `--dispatch-command`; the worker uses
the built-in Agent launcher automatically. The command-line flag and
`ATS_LAB_DISPATCH_COMMAND` remain explicit overrides.

Continuous workers synthesize through Agent when unresolved research chains
reach the low watermark (five by default). One planner lease emits exactly 25
chains: eligible controlled improvements first, at least five genuinely new
concepts, and no more than twenty improvements. A chain may contain significance
and gated baseline work, so 25 chains can create more than 25 work items. At most
three work items are ready/running; scheduled overflow is promoted as slots open.
HPO and paper-trade candidates remain promotion-locked. Use
`--no-idle-synthesis` for monitor-only waiting.

Each finished research response includes normalized run evidence and an
evaluation. Both persist together, letting the next cohort learn from metrics,
failure regimes and next steps without a separate evaluator context load.

```bash
ats-lab synthesis-status
```

For compute-rich/token-limited operation, configure `[resources]` in the ignored
`.ats-lab/config.toml`. See [resource policy](docs/resource-policy.md).

For each claimed item, the command receives a versioned execution request on
standard input. It must print one JSON object with an `outcome` of `finished`,
`retry`, or `blocked`. A retry may include `retry_after`; otherwise the worker
uses a 60-second delay. Blocked and retry results may include `blocker_code`
and `detail`. Finished research work must include `evidence.run` and
`evidence.evaluation`.

`--continuous` defaults off, `--idle-sleep` defaults to 30 seconds, and
`--retry-delay` defaults to 60 seconds. `--max-items N` provides a bounded run
for supervisors and smoke tests. The dispatch command can also be set with
`ATS_LAB_DISPATCH_COMMAND`; worker identity uses `ATS_LAB_WORKER_ID`.

The worker owns only queue lifecycle. The adapter owns agent launch and Jesse
operations, then returns a normalized outcome. Missing adapter configuration
fails before any work is claimed.

### Reconcile imported queue state

Preview classifications before starting workers:

```bash
ats-lab reconcile
```

Default stale threshold: 24 hours. Missing claim metadata on imported
`running` work also counts as stale. Apply conservative cleanup explicitly:

```bash
ats-lab reconcile --stale-after-hours 24 --apply
ats-lab normalize-blockers --apply
```

`normalize-blockers` returns never-attempted legacy ideas to `scheduled` while
preserving their former blocker text as readiness requirements. Runtime
`blocked` remains reserved for work that an executor actually attempted.

Apply blocks stale running work with `stale_worker_claim`. It archives only
`legacy_blocked` items already backed by an evaluation or terminal run. Other
blocked work remains actionable. Reconciliation never deletes records.

## Enqueue an experiment

Create `experiment.json`:

```json
{
  "schema_version": 1,
  "experiment": {
    "id": "BTC-TREND-001",
    "strategy_name": "ExampleTrendStrategy",
    "experiment_type": "baseline",
    "hypothesis": "A volatility-filtered trend entry persists after fees.",
    "archetype": "trend",
    "target_regime": "directional expansion",
    "failure_regime": "low-volatility chop",
    "routes": [
      {
        "exchange": "Example Perpetual Futures",
        "symbol": "BTC-USDT",
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "finish_date": "2024-12-31"
      }
    ],
    "success_gates": [
      {"name": "trade_count", "operator": ">=", "threshold": 30},
      {"name": "expectancy", "operator": ">", "threshold": 0}
    ],
    "failure_gates": [
      {"name": "max_drawdown", "operator": "<", "threshold": -30}
    ]
  },
  "work_item": {
    "id": "BTC-TREND-001",
    "experiment_id": "BTC-TREND-001",
    "priority": 20,
    "state": "ready",
    "dependencies": []
  }
}
```

Then enqueue it:

```bash
ats-lab enqueue --file experiment.json
```

The authoritative contract is
[`experiment-work-item.schema.json`](src/ats_lab/schemas/experiment-work-item.schema.json).

## Record an evaluation

```json
{
  "schema_version": 1,
  "experiment_id": "BTC-TREND-001",
  "verdict": "revise",
  "summary": "Baseline passed, but recent-window evidence is missing.",
  "metrics_summary": "trades=84 expectancy=2.1 max_drawdown=-11.4%",
  "next_step": "Run the unchanged strategy on the recent validation window.",
  "evaluator": "research-agent-1"
}
```

```bash
ats-lab evaluate --file evaluation.json
```

Research verdicts are independent from execution completion:

- `reject`
- `revise`
- `hpo_candidate`
- `paper_trade_candidate`
- `inconclusive`
- `infrastructure_failure`
- `pass`

## Work-item lifecycle

```text
scheduled -> ready -> running -> finished
                         |-> waiting_retry -> ready
                         |-> blocked
```

## Synthesize gated jobs

Create significance-first job chains for new or changed entries:

```bash
ats-lab synthesize --file idea.json
```

Unchanged entry rules with exit/sizing/risk-only changes skip the significance
test. Entry rules use stable fingerprints; `p_value < 0.05` unlocks baseline,
`0.05–0.10` holds it, and `> 0.10` archives it. See
[job synthesis](docs/synthesis.md).

Examples:

```bash
ats-lab finish BTC-TREND-001
ats-lab block BTC-TREND-001 --code missing_data --detail "Required route unavailable"
ats-lab retry BTC-TREND-001 --after 2026-08-01T00:00:00Z
```

## Legacy migration

The included adapter can import the original Markdown/YAML-block and JSON
research ledger used during development:

```bash
ats-lab --repo /path/to/legacy-repository migrate-legacy
ats-lab --repo /path/to/legacy-repository audit \
  --markdown /tmp/migration-audit.md
ats-lab --repo /path/to/legacy-repository inventory \
  --markdown /tmp/legacy-inventory.md
```

Migration is idempotent. Legacy deletion must wait until the audit reports no
unaccepted ambiguities. See [migration and cleanup](docs/migration-and-cleanup.md).

## Agent and memory integration

Recommended boundary:

```text
Agent: reasoning and orchestration
Memory provider: durable preferences and conclusions
Laboratory: operational state and research evidence
Execution framework: strategy code and backtests
```

Do not store queue locks, session IDs or authoritative metrics only in agent
memory. Agents restart; the laboratory database persists.

## Testing

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Public-release safety

Before publishing a migrated laboratory:

- exclude `.env`, credentials and exchange configuration;
- exclude private or licensed strategy source;
- exclude raw conversation/session dumps;
- review dashboard URLs and local filesystem paths;
- publish schemas and synthetic examples instead of private evidence;
- run secret scanning over the exact subtree or release archive.

## Contributing

Issues and small focused pull requests are welcome. Proposed execution adapters
should preserve the core rule: framework-specific operations stay behind the
adapter boundary, while specifications and evidence remain portable.

## Disclaimer

For research and software-engineering purposes only. Backtest results are not
financial advice and do not guarantee future performance. Validate data,
assumptions, fees, slippage and risk independently before any real-world use.

## License

MIT. See [LICENSE](LICENSE).
