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
ats-lab monitor --watch
ats-lab help
```

The installed CLI discovers the canonical ATS Lab checkout automatically.
Use `--repo` and `--database` only when intentionally targeting another lab.

## What to use

Use three interfaces:

| Interface | Purpose | Address or command |
|---|---|---|
| ATS supervisor | Run batch research loop | `research/automation/ats_lab.sh supervisor` |
| ATS terminal | Monitor and control loop | `research/automation/ats_lab.sh console` |
| ATS dashboard | Monitor queue, evidence, candidates, cohorts | <http://127.0.0.1:8765> |
| Jesse dashboard | Inspect individual backtest sessions | <http://127.0.0.1:9000> |

The `research/automation/ats_lab.sh` bridge remains supported for Jesse-side
compatibility, but normal operation should use the shorter `ats-lab` commands.

## How workflow works

```text
ATS SQLite ready queue
        |
        v
executor Agent turn
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
separate analyzer Agent turn
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
- Agent executor uses tools. Agent analyzer receives compact normalized
  evidence only and does not own workflow state.
- Markdown queues, journals and JSON sidecars are legacy evidence only.
- `ats-lab worker` is compatibility-only. Use `ats-lab supervisor`.

## Canonical evidence

Every completed run is normalized immediately into SQLite. Multi-route runs
become one row per atomic route and evidence split. Supervisor analysis, HPO,
deterministic gates, dashboard, CLI and Agent prompts all read these same rows.

Missing values remain SQL/JSON `null` internally and display as `—`. Currency
`net_profit` is never misread as `net_profit_percentage`. Raw Jesse metrics stay
available only through explicit diagnostic export.

Normal evidence view:

```bash
research/automation/ats_lab.sh evidence
research/automation/ats_lab.sh evidence \
  --symbol BTC-USDT \
  --timeframe 1h \
  --split oos \
  --rank sharpe_ratio
```

Machine-readable normalized evidence:

```bash
research/automation/ats_lab.sh evidence --format json
```

Raw diagnostic evidence for one known run:

```bash
research/automation/ats_lab.sh diagnostic-export RUN-ID
```

Raw optimizer parameters for one known HPO trial:

```bash
research/automation/ats_lab.sh diagnostic-hpo-trial HPO-STUDY-ID TRIAL-NUMBER
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
cd <repo-root>/src/repos/jesse-src/docker
docker compose up
```

Before starting supervisor, run deterministic stack gate:

```bash
PYTHONPATH=src python3 -m ats_lab.cli preflight
```

Gate checks Docker, Jesse PostgreSQL container/readiness/read-only query/public
schema, Jesse dashboard/MCP, then Memory. PostgreSQL inspection is limited to
`SELECT 1` and table names `candle`, `backtestsession`, and
`significancetestsession`; credential tables and values are never read.

Gate requires local Docker daemon, Jesse dashboard (`127.0.0.1:9000`), Jesse
MCP (`127.0.0.1:9002/mcp`), and Memory health API
(`127.0.0.1:18000/health`). Supervisor runs same gate before claiming work.
Failure returns `infrastructure_preflight_failed` without consuming strategy
attempts. Override only Memory URL with `ATS_LAB_MEMORY_HEALTH_URL`; keep
credentials in process environment, never tracked config or command output.

### 2. Inspect plan

```bash
cd <repo-root>/src/repos/jesse-src
research/automation/ats_lab.sh supervisor --plan
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
cd <repo-root>/src/repos/jesse-src
research/automation/ats_lab.sh dashboard --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>.

Keep dashboard loopback-only. It has no authentication.

### 4. Start terminal monitor

Use separate terminal:

```bash
research/automation/ats_lab.sh monitor --watch
```

Or open interactive control console:

```bash
research/automation/ats_lab.sh console
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
research/automation/ats_lab.sh supervisor
```

Continuous operation:

```bash
research/automation/ats_lab.sh supervisor --continuous
```

Bounded continuous run:

```bash
research/automation/ats_lab.sh supervisor \
  --continuous \
  --max-rounds 3
```

Prefer graceful CLI stop:

```bash
research/automation/ats_lab.sh control stop
```

Supervisor finishes current execution plus required analysis, then exits before
claiming another batch. Completed run evidence stays durable.

Resume after graceful stop:

```bash
research/automation/ats_lab.sh control resume
research/automation/ats_lab.sh supervisor --continuous
```

## Most effective operating pattern

Use this sequence:

1. Run `supervisor --plan`.
2. Resolve unhealthy claims or permanent blockers first.
3. Start ATS dashboard.
4. Run one supervisor round as smoke test.
5. Confirm completed runs and evaluations appear.
6. Start `supervisor --continuous`.
7. Run `monitor --watch` or `console` in another terminal.
8. Pause before inspecting or changing blocked requirements.
9. Use graceful `control stop`; do not kill active Jesse/Agent subprocesses.

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
research/automation/ats_lab.sh monitor
```

Refresh every five seconds:

```bash
research/automation/ats_lab.sh monitor --watch
```

Custom refresh:

```bash
research/automation/ats_lab.sh monitor --watch --interval 2
```

View includes durable control intent, supervisor phase/PID/heartbeat, queue
counts, active jobs, current batch, synthesis cohort, next action and top
candidates. It also shows HPO lifecycle counts, current analyzer state and most
recent stage duration.

### Supervisor control

```bash
research/automation/ats_lab.sh control status
research/automation/ats_lab.sh control pause
research/automation/ats_lab.sh control resume
research/automation/ats_lab.sh control stop
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
alias lab='<repo-root>/src/repos/jesse-src/research/automation/ats_lab.sh'
lab monitor --watch
lab control pause
lab control resume
lab console
```

### Compact health

```bash
research/automation/ats_lab.sh status
research/automation/ats_lab.sh status --format json
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
research/automation/ats_lab.sh queue
research/automation/ats_lab.sh queue --state ready
research/automation/ats_lab.sh queue --state blocked
research/automation/ats_lab.sh queue --format json
```

### Candidates

```bash
research/automation/ats_lab.sh candidates
research/automation/ats_lab.sh candidates --verdict hpo-candidate
research/automation/ats_lab.sh candidates --verdict paper-trade-candidate
research/automation/ats_lab.sh candidates --format json
```

Candidate views show one representative row per experiment. Use `evidence` to
inspect every atomic route/split row.

### HPO lifecycle

Use one lifecycle surface for HPO scheduling, execution, analysis, validation
and final disposition:

```bash
research/automation/ats_lab.sh hpo
research/automation/ats_lab.sh hpo --state hpo_running
research/automation/ats_lab.sh hpo-detail HPO-STUDY-ID
research/automation/ats_lab.sh analyzer
research/automation/ats_lab.sh timings
research/automation/ats_lab.sh timings --job HPO-WORK-ITEM-ID
research/automation/ats_lab.sh requeue-hpo-analysis HPO-ANALYSIS-JOB-ID \
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

Validation jobs keep trial parameters out of normal UI and analyzer payloads.
Executor hydrates selected parameters into execution-only context. Jobs remain
`requirements_pending` when canonical validation routes are absent; worker will
not claim them until symbol/timeframe/OOS or rolling periods are supplied.

Supply fresh split-specific routes without editing SQLite:

```bash
research/automation/ats_lab.sh configure-hpo-validation-routes \
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
    "finish_date": "2026-03-31"
  }],
  "rolling": [{
    "exchange": "Binance Perpetual Futures",
    "symbol": "BTC-USDT",
    "timeframe": "1h",
    "start_date": "2025-01-01",
    "finish_date": "2026-03-31"
  }]
}
```

Use genuinely unseen periods. Command validates routes, records operator event,
clears only matching `requirements_pending`, then normal promotion resumes.

### Audit

```bash
research/automation/ats_lab.sh audit
research/automation/ats_lab.sh synthesis-status
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
                         +-> blocked
```

| State | Meaning |
|---|---|
| `scheduled` | Future work waiting for dependency or ready capacity |
| `ready` | May be claimed by supervisor |
| `running` | Claimed execution or completed run awaiting batch analysis |
| `waiting_retry` | Transient failure with bounded backoff |
| `blocked` | Permanent constraint or human decision required |
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
research/automation/ats_lab.sh recover-claims --stale-after-hours 2
```

Apply only when preview shows abandoned claims with no durable runs:

```bash
research/automation/ats_lab.sh recover-claims \
  --stale-after-hours 2 \
  --apply
```

Completed executions awaiting analysis are excluded from stale-claim recovery.

### Analyzer failure

Do not rerun backtests manually. Restart same supervisor:

```bash
research/automation/ats_lab.sh supervisor
```

Supervisor finds durable runs marked `awaiting_batch_evaluation` and resumes
analysis.

### Retry loop

Inspect:

```bash
research/automation/ats_lab.sh queue
```

Retries use exponential delay and stop at configured attempt limit. Repeated
failure becomes `blocked` with `retry_limit_reached`.

### Blocked item

Inspect exact blocker:

```bash
research/automation/ats_lab.sh queue --state blocked
```

Do not blindly return blocked work to ready. Resolve requirement, accept explicit
research assumption, or archive with terminal evidence.

After fixing and validating root cause, reopen with durable evidence:

```bash
research/automation/ats_lab.sh resolve-blocker JOB-ID \
  --code sizing_fix_validated \
  --detail "Exact validated resolution." \
  --evidence JESSE-SESSION-ID
```

This atomically moves `blocked -> ready`, clears active blocker fields and
records previous blocker, resolution, detail and evidence IDs in SQLite events.

If durable run metrics exist but analyzer evidence was incomplete:

```bash
research/automation/ats_lab.sh requeue-evaluation JOB-ID \
  --batch RECOVERY-ID \
  --reason "Recover existing durable metrics."
```

This schedules analysis only. It does not rerun Jesse execution.

### Database audit or migration concern

Preview only:

```bash
research/automation/ats_lab.sh reconcile
research/automation/ats_lab.sh sanitize
```

Before applying sanitation, back up:

```bash
cp <repo-root>/src/repos/algorithmic-trading-strategy-laboratory/.ats-lab/laboratory.sqlite3 \
   <repo-root>/src/repos/algorithmic-trading-strategy-laboratory/.ats-lab/laboratory.sqlite3.backup
```

Then, only after reviewing preview:

```bash
research/automation/ats_lab.sh sanitize --apply
```

## Configuration

Local configuration:

```text
<repo-root>/src/repos/algorithmic-trading-strategy-laboratory/.ats-lab/config.toml
```

Example:

```toml
[repositories]
jesse = "<repo-root>/src/repos/jesse-src"

[executor]
executable = "<repo-root>/.local/bin/executor"
profile = "ats-lab"
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
research/automation/ats_lab.sh enqueue --file experiment.json
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
- Agent `ats-lab` profile
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
research/automation/ats_lab.sh supervisor --plan
research/automation/ats_lab.sh audit
git diff --check
```

## Reference documentation

- Architecture: `docs/architecture.md`
- Operator dashboard: `docs/operator-dashboard.md`
- Resource policy: `docs/resource-policy.md`
- Synthesis: `docs/synthesis.md`
- Agent launcher: `docs/executor-memory-launcher.md`
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
