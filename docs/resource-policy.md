# Resource policy

Resource policy separates expensive agent reasoning from abundant local
compute. Agent plans and handles ambiguous revisions; Jesse performs bulk
simulation locally.

```toml
[resources]
mode = "compute_heavy"
cpu_cores = 6
significance_simulations = 5000
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
active_ready_limit = 3
```

Compute-heavy behavior:

- One synthesis agent call returns exactly 25 chains at the five-chain watermark.
- Each cohort reserves at least five slots for new concepts and uses up to twenty
  eligible controlled improvements.
- Planner lease prevents duplicate synthesis across workers. Invalid batches
  wait five minutes before retry.
- Worker drains dependency-satisfied scheduled overflow as ready capacity opens,
  avoiding another synthesis context load between jobs in the cohort.
- Execution and evaluation share one Agent turn, avoiding a second context load.
- New entry jobs use 5,000 local random-entry simulations.
- HPO uses native Jesse optimization with 300 trials per parameter and keeps 50
  candidates.
- Candle Monte Carlo uses 500 scenarios.
- Six of ten CPU cores are available to Jesse; four remain for the OS, Docker,
  database and agent.
- Only three jobs may be ready/running, preventing stale speculative backlog.

These are execution budgets, not promotion criteria. More trials reduce search
noise but do not turn weak or overfit results into valid candidates.
