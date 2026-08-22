# Resource policy

Resource policy separates expensive agent reasoning from abundant local
compute. Agent plans and handles ambiguous revisions; Jesse performs bulk
simulation locally.

```toml
[resources]
mode = "compute_heavy"
cpu_cores = 6
significance_simulations = 5000
significance_fdr_level = 0.05
hpo_trials_per_parameter = 300
hpo_best_candidates = 50
monte_carlo_scenarios = 500
synthesis_inspect_limit = 25
synthesis_generate_limit = 25
synthesis_low_watermark = 5
synthesis_min_new_concepts = 5
synthesis_max_improvements = 20
synthesis_retry_cooldown_seconds = 300
synthesis_lease_seconds = 3600
claim_timeout_seconds = 7200
execution_batch_size = 8
active_ready_limit = 8
executor_infrastructure_failure_limit = 10

[resources.evaluation_windows]
# Relative defaults are resolved into explicit route dates at submission time.
# Use mode = "explicit" when every route must provide its own dates.
mode = "relative"
as_of_date = "2026-08-18"       # required ISO anchor for relative windows;
                                # wall-clock today is never used
comparison_lookback_days = 365
oos_lookback_days = 180
rolling_lookback_days = 90
```

Compute-heavy behavior:

- One synthesis agent call returns 25 chains at five-chain watermark. Context
  inspects at most 25 records and four canonical evidence rows per record.
- Local lane gate trims harmless over-generation to exact 25 while preserving
  minimum five new concepts and maximum twenty improvements. Under-generation
  fails explicitly.
- Each cohort reserves at least five slots for new concepts and uses up to twenty
  eligible controlled improvements.
- Planner lease prevents duplicate synthesis across workers. Invalid batches
  wait five minutes before retry.
- Worker drains dependency-satisfied scheduled overflow as ready capacity opens,
  avoiding another synthesis context load between jobs in the cohort.
- Up to eight jobs share one execution turn.
- One separate bounded analysis turn evaluates whole completed batch and creates
  next cohort when needed.
- New entry jobs use 5,000 local random-entry simulations.
- HPO uses native Jesse optimization with 300 trials per parameter and keeps 50
  candidates.
- Candle Monte Carlo uses 500 scenarios.
- Six of ten CPU cores are available to Jesse; four remain for the OS, Docker,
  database and agent.
- Only eight jobs may be ready/running, matching execution batch capacity.

These are execution budgets, not promotion criteria. More trials reduce search
noise but do not turn weak or overfit results into valid candidates.

`significance_fdr_level` is the Benjamini-Hochberg false-discovery-rate level
applied across one synthesis cohort's entry-significance family (see
[synthesis](synthesis.md)). `executor_infrastructure_failure_limit` is the
circuit breaker: that many consecutive uncharged infrastructure failures on one
work item block it as `infrastructure_circuit_broken` instead of retrying
forever.

Evaluation windows are policy defaults, not hidden mutable state. A relative
policy must be resolved when routes are created, and the resulting
`start_date`/`finish_date` remain on each route for reproducibility. Relative
resolution requires an explicit anchor (`as_of_date` or an explicit
`anchor_date` argument); unanchored resolution is rejected rather than falling
back to wall-clock today. Adjacent windows are half-open — no shared candle
days — because each window's stored finish date is the day before the next
window starts. Explicit route dates remain fully supported for legacy jobs and
controlled reruns.
