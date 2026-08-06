# Operator dashboard

The local dashboard is a read-only view of the SQLite laboratory database. It
shows active work, running and blocked counts, research candidates, canonical
normalized evidence and dashboard links. It refreshes automatically every five seconds while
the tab is visible; the header controls can pause it or select 15/30 seconds.

Start it from the repository root:

```bash
ats-lab dashboard --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>. Use a different database or port when needed:

```bash
python3 -m ats_lab.dashboard \
  --database .ats-lab/laboratory.sqlite3 \
  --host 127.0.0.1 \
  --port 9001
```

Views support free-text search, page-specific filters and safe, predefined sort
orders:

- **Active queue** — future and actionable work, including worker claims,
  retries and blocker details.
- **Candidates** — HPO, paper-trade and revision candidates using canonical
  `NormalizedEvidence`, deduplicated to one representative row per experiment.
- **Run history** — standardized route/split evidence with run and session IDs.
- **HPO lifecycle** — study scheduling, optimizer progress, analyzer state,
  selected trials, validation status and final disposition.
- **Overview** — live queue/candidate counts and a horizontal chart of the top
  20 finished backtests. Rank by Sharpe, net profit, Calmar, profit factor,
  drawdown or expectancy, and set a minimum trade count to suppress tiny samples.

Primary tables show strategy/version, lifecycle/verdict, market/period/split,
net profit, drawdown, Sharpe, trades, finding and next action. Expanded rows show
the remaining standardized IDs, ratios, fees, risk, leverage, optimizer,
cost-stress and completion fields.

HPO primary rows show shared lifecycle label, strategy/study, objective, completed
and total trials, selected count, validation count, disposition and next action.
Open a study for normalized evidence/session links, analyzer state and per-stage
timings. Lifecycle labels are:

- `hpo_candidate`
- `hpo_scheduled`
- `hpo_running`
- `hpo_analysis`
- `validation`
- `paper_trade_candidate`
- `revise`
- `reject`

Missing values display as `—`. No normal page or API returns raw run JSON.
Comparison defaults to the newest record with a complete compatibility key and
then matches exact symbol, timeframe, start date, finish date and evidence split.
Changing rank metric changes ordering only; every standardized metric remains
visible.

Read-only JSON endpoints are available for scripts and alternate frontends:

- `/api/summary`
- `/api/top-backtests?metric=sharpe&limit=20&minimum_trades=20`
- `/api/queue`, `/api/candidates`, `/api/runs` (accept the same filters as pages)
- `/api/synthesis-status` (remaining chains plus latest cohort lease/failure state)
- `/api/hpo-studies` and `/api/hpo-studies/HPO-STUDY-ID`
- `/api/analyzer-status`
- `/api/lifecycle-timings?work_item_id=JOB-ID`

Explicit diagnostic-only endpoint:

- `/api/diagnostics/runs/RUN-ID`
- `/api/diagnostics/hpo/HPO-STUDY-ID/trials/TRIAL-NUMBER`

Do not use diagnostic payloads as dashboard, HPO, gate or analyzer contracts.

Terminal supervision:

```bash
ats-lab status
ats-lab evidence
ats-lab candidates
ats-lab hpo
ats-lab hpo-detail HPO-STUDY-ID
ats-lab hpo-route-plan HPO-STUDY-ID
ats-lab analyzer
ats-lab timings
ats-lab requeue-hpo-analysis HPO-ANALYSIS-JOB-ID \
  --reason "provider or transport blocker repaired"
ats-lab configure-hpo-validation-routes HPO-STUDY-ID \
  --file validation-routes.json
ats-lab supervisor --plan
```

Route files may include `hpo`, `oos`, and `rolling` entries. An explicit
`hpo` route is required to release a route-less optimizer; OOS and rolling
routes release only their matching validation jobs and are never reused for
optimizer execution.

`hpo-route-plan` is read-only. It shows the three required roles (optimizer
training, unseen holdout, and unseen rolling validation), configured counts,
known route shapes observed for the strategy, and the exact configuration
command. Known route shapes are evidence only; the operator must choose dates
from verified Jesse candle availability. Route configuration rejects malformed
date ranges and overlapping HPO training versus OOS/rolling periods for the
same exchange, symbol, and timeframe.

An HPO execution that returns no durable completed trials is parked with
`hpo_trials_required` and readiness `requirements_pending`. This is an explicit
external-optimizer handoff: import the completed Optuna study before resuming
analysis. The supervisor will not repeatedly retry an empty analyzer payload.

These commands render human tables by default. Add `--format json` only for
machine-readable normalized/status data.

`status` reports scheduled/ready/running/retry/blocked/finished counts, completed
executions awaiting batch analysis, HPO lifecycle counts, analyzer state, recent
stage timing, oldest unresolved claim, cohort state and one recommended next
action. `supervisor --plan` adds active execution/synthesis policy. Both are
read-only.

`requeue-hpo-analysis` is an explicit recovery control, not an automatic retry.
It accepts only terminal jobs, records reason/operator in event log, resets
attempt budget, and returns study to `hpo_analysis`.

Recover only stale claims lacking durable runs:

```bash
ats-lab recover-claims --stale-after-hours 2
ats-lab recover-claims --stale-after-hours 2 --apply
```

Always inspect preview first. Completed executions awaiting analysis are excluded.

The server has no write endpoints. Queries use bound parameters and predefined
sort expressions; database content is HTML-escaped before rendering. Responses
disable caching and include restrictive browser security headers.

The default bind address is loopback-only. Do not bind it to `0.0.0.0` on an
untrusted network: the server has no authentication and research evidence may be
sensitive.

Stop it with `Ctrl-C`.
