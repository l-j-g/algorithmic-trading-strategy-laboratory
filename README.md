# ATS Lab User Manual

ATS Lab runs evidence-first Jesse strategy research in batches.

It does not trade live, manage exchange credentials, or guarantee profitable
strategies.

## Start here

Run this from any directory:

```bash
ats-lab
```

It shows current health, supervisor state, queue, memory, and one exact next
command. For deeper checks or live progress:

```bash
ats-lab doctor
ats-lab next
ats-lab tui
ats-lab monitor --watch
ats-lab help
```

`ats-lab tui` is the normal interactive view: color status, responsive columns,
constant refresh, numbered views, arrow-key navigation, selected-row details,
and keyboard supervisor controls. Press `?` inside it for keys and `q` to exit.
The plain `monitor --watch` stream remains available for logs, piping, and
minimal terminals.

See [terminal UI](docs/terminal-ui.md) for keys and reusable component
boundaries. See [maintainability](docs/maintainability.md) for SOLID conventions
and bounded refactoring order.

The installed CLI discovers the canonical ATS Lab checkout automatically.
Use `--repo` and `--database` only when intentionally targeting another lab.

## Repository layout

Three repositories have separate jobs:

| Repository | Owns | Update rule |
|---|---|---|
| `jesse-upstream` | clean public Jesse engine checkout | fetch and fast-forward only |
| `jesse-src` | private strategies, routes, config, candles, research notes | research agents edit through Jesse worktrees |
| `algorithmic-trading-strategy-laboratory` | ATS code, SQLite queue/evidence, contracts, operators | normal feature worktrees |

ATS-owned Jesse research policy lives in:

- `docs/jesse/BACKTEST_EVALUATION_PROTOCOL.md`
- `docs/jesse/STRATEGY_CONCEPT_PLAYBOOK.md`

Use one control surface from ATS Lab:

```bash
scripts/jesse-workspace.sh status
scripts/jesse-workspace.sh upstream update
scripts/jesse-workspace.sh image build
scripts/jesse-workspace.sh stack up
scripts/jesse-workspace.sh stack up --no-update  # deliberate pinned/offline start
```

`stack up` polls the public upstream first, then starts Jesse from the
commit-tagged immutable image. `image build` can still be run separately. Use
`stack up --no-update` only for a deliberate pinned/offline start. The default
upstream checkout is
`<workspace-root>/jesse-upstream`; override with
`JESSE_UPSTREAM_REPOSITORY` when needed. Do not use `salehmir/jesse:latest`
for a research run when commit provenance matters.

For recurring polling while the stack is not being restarted, schedule
`scripts/jesse-workspace.sh upstream refresh`. It fast-forwards the public
mirror and builds only when the corresponding commit-tagged image is absent.

`jesse-src` remains the configured research workspace repository because it
contains the research workspace. The Jesse engine inside the container comes
from the clean public upstream checkout. See
[workspace integration](docs/workspace-integration.md).

## What to use

Use three interfaces:

| Interface | Purpose | Address or command |
|---|---|---|
| ATS supervisor | Run batch research loop | `ats-lab supervisor` |
| ATS terminal | Monitor and control loop | `ats-lab console` |
| ATS dashboard | Monitor queue, evidence, candidates, cohorts | <http://127.0.0.1:8765> |
| Jesse dashboard | Inspect individual backtest sessions | <http://127.0.0.1:9000> |

The old Jesse-side shell bridge is retired. Use the installed `ats-lab` CLI
from either repository; it discovers the canonical ATS checkout and database.

## How workflow works

```text
ATS SQLite ready queue
        |
        v
executor agent turn
  runs up to 8 jobs through Jesse MCP
        |
        v
durable run evidence in SQLite
        |
        v
canonical NormalizedEvidence rows
  one record per route/split
        |
        v
deterministic gates
  same fields used by every consumer
        |
        v
separate analyzer agent turn
  evaluates whole completed batch once
        |
        v
validated evaluations
        |
        +-- remaining chains > 5 --> execute next batch
        |
        +-- remaining chains <= 5 -> synthesize 25 new chains
```

Important boundaries:

- ATS SQLite is sole queue, run, evaluation and synthesis authority.
- `NormalizedEvidence` is sole metric contract. `CandidateMetrics` is an alias,
  not another schema.
- Jesse owns strategy source, candles and backtest execution.
- Trading operations use Jesse MCP.
- The executor agent uses tools. The analyzer agent receives compact normalized
  evidence only and does not own workflow state.
- Markdown queues, journals and JSON sidecars are legacy evidence only.
- `ats-lab worker` is compatibility-only. Use `ats-lab supervisor`.

## Canonical evidence

Every completed run is normalized immediately into SQLite. Multi-route runs
become one row per atomic route and evidence split. Supervisor analysis, HPO,
deterministic gates, dashboard, CLI and agent prompts all read these same rows.

Missing values remain SQL/JSON `null` internally and display as `—`. Currency
`net_profit` is never misread as `net_profit_percentage`. Raw Jesse metrics stay
available only through explicit diagnostic export.

Normal evidence view:

```bash
ats-lab evidence
ats-lab evidence \
  --symbol BTC-USDT \
  --timeframe 1h \
  --split oos \
  --rank sharpe_ratio
```

Machine-readable normalized evidence:

```bash
ats-lab evidence --format json
```

Raw diagnostic evidence for one known run:

```bash
ats-lab diagnostic-export RUN-ID
```

Raw optimizer parameters for one known HPO trial:

```bash
ats-lab diagnostic-hpo-trial HPO-STUDY-ID TRIAL-NUMBER
```

Do not use diagnostic output for ranking, gating, HPO analysis or normal
monitoring.

## Daily operation

### 1. Check Jesse

Jesse dashboard should open:

<http://127.0.0.1:9000>

Jesse MCP endpoint:

```text
http://127.0.0.1:9002/mcp
```

If Jesse is stopped:

```bash
cd "$JESSE_RESEARCH_REPOSITORY/docker"
docker compose up
```

Before starting supervisor, run deterministic stack gate:

```bash
PYTHONPATH=src python3 -m ats_lab.cli preflight
```

Gate checks Docker, Jesse PostgreSQL container/readiness/read-only query/public
schema, Jesse dashboard/MCP, then the memory provider. PostgreSQL inspection is
limited to `SELECT 1` and table names `candle`, `backtestsession`, and
`significancetestsession`; credential tables and values are never read.

Gate requires local Docker daemon, Jesse dashboard (`127.0.0.1:9000`), Jesse
MCP (`127.0.0.1:9002/mcp`), and memory provider health API
(`127.0.0.1:18000/health`). Supervisor runs same gate before claiming work.
Failure returns `infrastructure_preflight_failed` without consuming strategy
attempts. Override only the memory provider URL with
`ATS_LAB_MEMORY_HEALTH_URL`; keep credentials in process environment, never
tracked config or command output.

### 2. Inspect plan

```bash
cd "$JESSE_RESEARCH_REPOSITORY"
ats-lab supervisor --plan
```

This command is read-only. Check:

- `healthy` is `true`;
- `awaiting_batch_evaluation` is `0`, unless resuming analysis;
- `unresolved_execution_claims` is `0`; active work appears separately under
  `running_execution_claims`;
- `next_action` explains expected supervisor action;
- configured `execution_batch_size` is `8`;
- synthesis limit is `25`;
- synthesis low watermark is `5`.

### 3. Start dashboard

Use separate terminal:

```bash
cd "$JESSE_RESEARCH_REPOSITORY"
ats-lab dashboard --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>.

Keep dashboard loopback-only. It has no authentication.

### 4. Start terminal monitor

Use separate terminal:

```bash
ats-lab monitor --watch
```

Or open interactive control console:

```bash
ats-lab console
```

Console commands:

```text
status
watch [seconds]
queue [state]
candidates
evidence
hpo
analyzer
timings
pause
resume
stop
help
quit
```

`quit` closes console only. It does not stop supervisor.

### 5. Run research

One supervisor round:

```bash
ats-lab supervisor
```

Continuous operation:

```bash
ats-lab loop start
```

Inspect, pause, or stop it from any directory:

```bash
ats-lab loop status
ats-lab loop pause
ats-lab loop stop
```

Bounded continuous run:

```bash
ats-lab supervisor \
  --continuous \
  --max-rounds 3
```

Prefer graceful CLI stop:

```bash
ats-lab loop stop
```

Supervisor finishes current execution plus required analysis, then exits before
claiming another batch. Completed run evidence stays durable.

Start again after graceful stop:

```bash
ats-lab loop start
```

## Most effective operating pattern

Use this sequence:

1. Run `supervisor --plan`.
2. Resolve unhealthy claims or permanent blockers first.
3. Start ATS dashboard.
4. Run one supervisor round as smoke test.
5. Confirm completed runs and evaluations appear.
6. Run `ats-lab loop start`.
7. Run `monitor --watch` or `console` in another terminal.
8. Pause before inspecting or changing blocked requirements.
9. Use graceful `ats-lab loop stop`; do not kill active Jesse/executor subprocesses.

Avoid:

- running legacy Markdown discovery loop;
- running `ats-lab worker` beside supervisor;
- launching multiple supervisors with different worker IDs;
- editing SQLite directly;
- deleting/recreating database to clear state;
- treating profitable single backtest as promotion evidence;
- storing queue state only in chat or memory.

## Monitoring commands

### Recommended terminal view

One snapshot:

```bash
ats-lab monitor
```

Refresh every five seconds:

```bash
ats-lab monitor --watch
```

Custom refresh:

```bash
ats-lab monitor --watch --interval 2
```

View includes durable control intent, supervisor phase/PID/heartbeat, queue
counts, active jobs, current batch, synthesis cohort, next action and top
candidates. It also shows HPO lifecycle counts, current analyzer state and most
recent stage duration.

### Supervisor control

```bash
ats-lab control status
ats-lab control pause
ats-lab control resume
ats-lab control stop
```

Semantics:

| Command | Effect |
|---|---|
| `pause` | Finish current batch and analysis; remain alive; claim no new work |
| `resume` | Allow paused supervisor to claim next batch |
| `stop` | Finish current batch and analysis; exit before next claim |
| `status` | Show durable request plus reported supervisor runtime |

Control state lives in canonical SQLite. Reopening terminal does not lose it.
`stop` remains requested until `resume`; run `resume` before starting another
supervisor.

Optional short alias for current shell:

```bash
alias lab='ats-lab'
lab monitor --watch
lab control pause
lab control resume
lab console
```

### Compact health

```bash
ats-lab status
ats-lab status --format json
```

Important fields:

| Field | Meaning |
|---|---|
| `healthy` | No unresolved execution claim |
| `next_action` | Recommended operator action |
| `awaiting_batch_evaluation` | Runs finished but analyzer not yet durable |
| `running_execution_claims` | Current executor batch claims |
| `unresolved_execution_claims` | Stale claims past configured timeout |
| `remaining_chains` | Unresolved research chains before refill |
| `latest_event` | Most recent durable workflow event |

### Active queue

```bash
ats-lab queue
ats-lab queue --state ready
ats-lab queue --state blocked
ats-lab queue --format json
```

### Candidates

```bash
ats-lab candidates
ats-lab candidates --verdict hpo-candidate
ats-lab candidates --verdict paper-trade-candidate
ats-lab candidates --format json
```

Candidate views show one representative row per experiment. Use `evidence` to
inspect every atomic route/split row.

### HPO lifecycle

Use one lifecycle surface for HPO scheduling, execution, analysis, validation
and final disposition:

```bash
ats-lab hpo
ats-lab hpo --state hpo_running
ats-lab hpo-detail HPO-STUDY-ID
ats-lab analyzer
ats-lab timings
ats-lab timings --job HPO-WORK-ITEM-ID
ats-lab requeue-hpo-analysis HPO-ANALYSIS-JOB-ID \
  --reason "provider or transport blocker repaired"
```

Normal commands render human tables. Add `--format json` for machine-readable
lifecycle/status data. Terminal analyzer jobs require explicit operator requeue
with a durable reason; attempts reset only after that command.

Lifecycle labels:

| Label | Meaning |
|---|---|
| `hpo_candidate` | Baseline evidence approved for optimization |
| `hpo_scheduled` | HPO work created; waiting for execution |
| `hpo_running` | Optimizer study running |
| `hpo_analysis` | Study complete; selected-trial analysis pending/running |
| `validation` | Selected trials undergoing holdout/OOS/rolling validation |
| `paper_trade_candidate` | Validation supports paper-trade review |
| `revise` | Evidence supports one controlled revision |
| `reject` | Evidence does not support further promotion |

`hpo-detail` shows study objective, trial progress, selected trials, validation
status, analyzer state, stage durations, and normalized run/session evidence
links. It never prints raw optimizer JSON. Use `diagnostic-hpo-trial` only when
investigating optimizer internals.

Scheduled HPO execution does not invent trial rows. If the optimizer returns a
completed run without durable trial/import evidence, ATS Lab parks analysis with
`hpo_trials_required` and readiness `requirements_pending`; it will not spend
analyzer retries on an empty payload. Import a completed Optuna study or a
versioned, complete Jesse per-trial session export through the HPO import
workflow, then resume the resulting `hpo_analysis` job. Jesse dashboard
`best_candidates` summaries remain partial and are rejected.

Validation jobs keep trial parameters out of normal UI and analyzer payloads.
Executor hydrates selected parameters into execution-only context. Jobs remain
`requirements_pending` when canonical validation routes are absent; worker will
not claim them until symbol/timeframe/OOS or rolling periods are supplied.

Supply fresh split-specific routes without editing SQLite:

```bash
ats-lab configure-hpo-validation-routes \
  OPTUNA-9BD60A3E3546 --file validation-routes.json
```

`validation-routes.json`:

```json
{
  "oos": [{
    "exchange": "Binance Perpetual Futures",
    "symbol": "BTC-USDT",
    "timeframe": "1h",
    "start_date": "2026-01-01",
    "finish_date": "2026-04-01"
  }],
  "rolling": [{
    "exchange": "Binance Perpetual Futures",
    "symbol": "BTC-USDT",
    "timeframe": "1h",
    "start_date": "2025-01-01",
    "finish_date": "2026-01-01"
  }]
}
```

For a quick local bootstrap, ATS Lab includes a conservative, disjoint
BTC-USDT 1h policy (`hpo` 2024-01-01..2025-01-01, `rolling`
2025-01-01..2026-01-01, `oos` 2026-01-01..2026-04-01). Preview it with
`ats-lab hpo-defaults`; apply it only to untouched scheduled studies with
`ats-lab hpo-defaults --apply`. Verify candle availability before production
research; explicit route files always take precedence.

For a newly scheduled HPO study with no inherited training route, include an
explicit `hpo` entry in the same file.  HPO execution never reuses OOS or
rolling routes; those splits release validation jobs only:

```json
{
  "hpo": [{
    "exchange": "Binance Perpetual Futures",
    "symbol": "BTC-USDT",
    "timeframe": "1h",
    "start_date": "2024-01-01",
    "finish_date": "2025-01-01"
  }]
}
```

Use genuinely unseen periods. Command validates routes, records operator event,
clears only matching `requirements_pending`, then normal promotion resumes.

### Audit

```bash
ats-lab audit
ats-lab synthesis-status
```

Healthy audit expectations:

- `finished_missing_evaluation: 0`;
- `unknown_strategy: 0`;
- no unexplained running claim;
- no completed batch left unevaluated indefinitely.

## Work states

```text
scheduled -> ready -> running -> finished
                         |
                         +-> waiting_retry -> ready
                         |
                         +-> failed run -> analysis -> finished
                         |
                         +-> blocked (operator requirement only)
```

| State | Meaning |
|---|---|
| `scheduled` | Future work waiting for dependency or ready capacity |
| `ready` | May be claimed by supervisor |
| `running` | Claimed execution or completed run awaiting batch analysis |
| `waiting_retry` | Transient failure with bounded backoff |
| `blocked` | External constraint or explicit human decision required |
| `finished` | Execution and evaluation durable |
| `archived` | Terminal history, not future work |

Execution completion and research verdict are separate facts.

Verdicts:

- `reject`
- `revise`
- `inconclusive`
- `pass`
- `hpo_candidate`
- `paper_trade_candidate`
- `infrastructure_failure`

## Recovery

### Stale claim

Always preview:

```bash
ats-lab recover-claims --stale-after-hours 2
```

Apply only when preview shows abandoned claims with no durable runs:

```bash
ats-lab recover-claims \
  --stale-after-hours 2 \
  --apply
```

Completed executions awaiting analysis are excluded from stale-claim recovery.

### Analyzer failure

Do not rerun backtests manually. Restart same supervisor:

```bash
ats-lab supervisor
```

Supervisor finds durable runs marked `awaiting_batch_evaluation` and resumes
analysis.

### Retry loop

Inspect:

```bash
ats-lab queue
```

Transient infrastructure failures use bounded backoff without changing research
quality. Terminal strategy or harness failures persist as failed-run evidence
and pass directly to isolated analysis. Analysis returns `revise` for one
bounded fix or parameter change, otherwise `reject`. Both finish the original
work item; neither remains an active queue blocker.

Legacy retry-limit and strategy/harness blocker rows are recovered into this
analysis path in bounded cohorts. Do not manually requeue them for another
identical execution.

### Blocked item

Inspect exact blocker:

```bash
ats-lab queue --state blocked
```

Do not blindly return blocked work to ready. Resolve requirement, accept explicit
research assumption, or archive with terminal evidence.

After fixing and validating root cause, reopen with durable evidence:

```bash
ats-lab resolve-blocker JOB-ID \
  --code sizing_fix_validated \
  --detail "Exact validated resolution." \
  --evidence JESSE-SESSION-ID
```

This atomically moves `blocked -> ready`, clears active blocker fields and
records previous blocker, resolution, detail and evidence IDs in SQLite events.

If durable run metrics exist but analyzer evidence was incomplete:

```bash
ats-lab requeue-evaluation JOB-ID \
  --batch RECOVERY-ID \
  --reason "Recover existing durable metrics."
```

This schedules analysis only. It does not rerun Jesse execution.

### Database audit or migration concern

Preview only:

```bash
ats-lab reconcile
ats-lab sanitize
```

Before applying sanitation, back up:

```bash
cp .ats-lab/laboratory.sqlite3 .ats-lab/laboratory.sqlite3.backup
```

Then, only after reviewing preview:

```bash
ats-lab sanitize --apply
```

## Configuration

Local configuration:

```text
<repo-root>/.ats-lab/config.toml
```

Example:

```toml
[repositories]
jesse = "<path-to-jesse-research-workspace>"

[executor]
executable = "<your-agent-executor-binary>"
# profile = "<optional executor profile>"
timeout_seconds = 3600

[resources]
mode = "compute_heavy"
cpu_cores = 6
significance_simulations = 5000
hpo_trials_per_parameter = 300
hpo_best_candidates = 50
monte_carlo_scenarios = 500
execution_batch_size = 8
active_ready_limit = 8
synthesis_inspect_limit = 25
synthesis_generate_limit = 25
synthesis_low_watermark = 5
synthesis_min_new_concepts = 5
synthesis_max_improvements = 20
synthesis_retry_cooldown_seconds = 300
synthesis_lease_seconds = 3600
claim_timeout_seconds = 7200
```

Recommended token-efficient settings:

- execute 8 jobs per executor turn;
- analyze completed batch once;
- generate 25 chains per synthesis turn;
- inspect at most 25 synthesis records and 4 canonical evidence rows per record;
- refill at 5 remaining chains;
- require at least 5 new concepts;
- allow up to 20 controlled improvements.

## Manual experiment enqueue

Normal operation should use synthesis. For explicit operator-created work,
create `experiment.json`:

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
    "routes": [{
      "exchange": "Binance Perpetual Futures",
      "symbol": "BTC-USDT",
      "timeframe": "1h",
      "start_date": "2024-01-01",
      "finish_date": "2024-12-31"
    }]
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

Enqueue:

```bash
ats-lab enqueue --file experiment.json
```

## Installation

Fresh checkout:

```bash
git clone https://github.com/l-j-g/algorithmic-trading-strategy-laboratory.git
cd algorithmic-trading-strategy-laboratory
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
ats-lab init
```

Requirements:

- Python 3.11+
- an agent executor binary for analysis/synthesis tasks (execution-only mode
  needs no agent executor)
- running Jesse service and MCP
- local `.ats-lab/config.toml`

## Testing

From ATS Lab repository:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
git diff --check
```

From Jesse repository:

```bash
ats-lab supervisor --plan
ats-lab audit
git diff --check
```

## Reference documentation

- Architecture: `docs/architecture.md`
- Operator dashboard: `docs/operator-dashboard.md`
- Resource policy: `docs/resource-policy.md`
- Synthesis: `docs/synthesis.md`
- Agent launcher: `docs/agent-launcher.md`
- Jesse integration: `docs/workspace-integration.md`
- Migration cleanup: `docs/migration-and-cleanup.md`

## Safety

- Research/backtesting only.
- No live order placement.
- No exchange credentials in ATS Lab.
- Keep dashboard bound to `127.0.0.1`.
- Preserve licensed/private strategy-source boundaries.
- Backtests are not financial advice and do not guarantee future performance.

## License

MIT. See `LICENSE`.
