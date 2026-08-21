# Backtest Evaluation Protocol

> Current evaluation gates. ATS Lab SQLite owns queue, claims, runs, evidence,
> evaluations, HPO, and synthesis. The Markdown queue and journal below are
> retained historical ledgers; do not use them as the active control plane.

Date created: 2026-06-02
Scope: Jesse research/backtesting only. No live trading recommendation.

## Purpose

Keep every strategy evaluation thesis-led, comparable, and resistant to overfitting to one time period or market regime.

Every evaluation must produce:

- strategy
- market thesis
- expected regime
- failure regime
- exchange / symbol / timeframe / date ranges
- leverage
- sizing and cost assumptions
- MCP session ID and dashboard URL
- metrics
- route/regime comparison
- verdict: reject, revise, hpo-candidate, or paper-trade candidate

## Core Anti-Overfitting Rule

No strategy advances from one profitable backtest. A strategy must survive multiple comparison windows and market regimes before HPO.

## Entry Significance Testing

Entry-rule significance protects against reading noise as edge. Two
methodology rules are enforced in code:

1. **First test wins.** For each canonical entry fingerprint, the earliest
   finished significance run is binding. Later re-tests of the same entry
   rule are stored and visible but never flip baseline readiness; the
   synthesis status output reports which run counted (`run_id`, `p_value`,
   `decided_at`). Re-running significance until p < 0.05 is not a valid
   path to release.
2. **Benjamini-Hochberg FDR control per synthesis cohort.** All entry
   significance p-values inside one synthesis cohort form one hypothesis
   family. Once every member has a binding finished test, the family is
   evaluated with the Benjamini-Hochberg step-up procedure at
   `resources.significance_fdr_level` (default 0.05): sort the p-values
   ascending and reject the largest rank k satisfying p(k) <= k * q / m;
   every hypothesis at or below that rank is rejected. A baseline is
   released only when its raw p < 0.05 AND Benjamini-Hochberg rejects it.
   Raw-significant but non-rejected members stay scheduled as
   `significance_withheld_bh_fdr`; the effective per-hypothesis threshold,
   rank, family size, and rejection flag are persisted on the dependent
   work item under `gate_findings` so every verdict remains explainable.
   Cohorts with unfinished members wait (`awaiting_cohort_fdr`) instead of
   releasing early. Significance tests outside a synthesis cohort keep the
   single-test raw-p gates. Inconclusive results (0.05 <= p <= 0.10) are
   their own verdict on every path: dependent baselines stay scheduled
   pending more evidence, and archiving remains a manual operator action.

## Evaluation Window Policy

Do not treat calendar dates in this document as permanent defaults. Configure
relative windows under `[resources.evaluation_windows]`, resolve them at route
creation time, and persist the resulting explicit `start_date` and
`finish_date` on every route. This avoids calendar drift while preserving
reproducible reruns. Set `mode = "explicit"` for legacy jobs or research that
requires caller-owned dates.

The default policy uses a 365-day comparison lookback, a 180-day OOS
lookback, and a 90-day rolling lookback. These are policy examples, not
mandatory dates. Regime labels and boundaries must be documented with the
route set or data-derived regime definition.

Never tune on the OOS or rolling validation routes.

## Baseline Route Set

Initial route set:

- Exchange: Binance Perpetual Futures
- Symbols: BTC-USDT, ETH-USDT, SOL-USDT
- Timeframes: 1h, 4h
- Leverage: 1x first
- Cost: exchange fee from Jesse config, currently 0.0004 unless changed and documented

Only test 2x and 3x after 1x liquidation/drawdown buffer review.

## Required Metrics

Record per run and, when possible, per route:

- total trades
- net profit
- net profit percentage
- annual return
- max drawdown
- Sharpe
- Sortino
- Calmar
- win rate
- gross profit
- gross loss
- profit factor
- expectancy
- expectancy percentage
- fees
- average trades per month
- longs/shorts count
- benchmark result if available

## Pass / Fail Gates

### Immediate reject

Reject if any of these hold:

- negative expectancy after fees across the main comparison window
- drawdown above 30% at 1x without exceptional route robustness
- strategy only works on one symbol and fails the others badly
- one isolated trade or one narrow period explains most profits
- trade count is too low to evaluate and the thesis needs frequency
- performance collapses under 2x fee sensitivity

### Revise

Revise if:

- thesis has some signal but poor route robustness
- drawdown is too high relative to return
- trade count is low but signal quality is plausible
- one symbol/timeframe works and others fail, suggesting regime or route filter needed

Trade-count interpretation is window-normalized. A short 90-day or
low-frequency 4h study is not rejected merely because it cannot produce 100
trades. The hard floor is only a sample-adequacy screen; significance,
independent windows, fees, drawdown, and liquidation behavior remain required.

### HPO candidate

Only consider HPO if:

- baseline is positive after fees
- every dated route/window clears the normalized activity floor:
  `max(12, ceil(20 * window_days / 365.25))` trades; legacy rows without
  complete dates use the configured fallback
- at least two independent windows are positive or flat when the strategy is
  low-frequency; trade count alone never substitutes for route/regime evidence
- multiple windows are positive or flat rather than catastrophic
- no single route dominates all performance
- fee sensitivity does not destroy the edge

These criteria are enforced deterministically in code (`evaluate_hpo_candidate`):
missing evidence yields an inconclusive verdict and never unlocks an
optimization cycle, regardless of any agent-assigned `hpo_candidate` label.

### Paper-trade candidate

Only after:

- passing OOS evidence with complete explicit candle route and metrics
- passing rolling walk-forward evidence with complete explicit candle route
  and metrics
- passing candles-based Monte Carlo/path robustness with route dates and
  numeric path metrics; prose, labels, or arbitrary run names do not count
- fee/cost stress pass
- reasonable drawdown and liquidation buffer
- explicit research conclusion, not a live recommendation

## Workflow For Every Backtest

1. State hypothesis and expected outcome before running.
2. State target regime and failure regime.
3. Choose one controlled change.
4. Create or update the work item through ATS Lab. Do not add active work to
   the retired Markdown queue.
5. Run via Jesse MCP.
6. Poll until terminal state.
7. If finished, extract metrics and dashboard URL.
8. If stopped, log exception and blocker.
9. Persist the result and evaluation through ATS Lab.
10. Update the relevant optional narrative file under
    `<jesse-src>/research/experiments/` only when a
    human-readable record is useful.
11. Assign verdict.
12. If the finding is durable and non-transient, save a compact memory.

## Journal Entry Schema

Historical `<jesse-src>/research/RESEARCH_JOURNAL.md`
entries use **YAML list blocks** (not markdown tables). New canonical evidence
belongs in ATS Lab. Minimum fields for an optional narrative entry:

```yaml
- rank: 2
  id: EC-V7-PREHPO-003
  status: complete
  strategy: EmaConvictionTrendV7
  thesis: ...
  route: ...
  windows: [...]
  sessions: {...}
  results: {...}
  verdict: hpo-candidate
  next_step: ...
  log: <jesse-src>/research/experiments/...
```

## Job Queue Schema

Historical `<jesse-src>/research/TEST_JOB_QUEUE.md` uses
**YAML list blocks** under section headings. It is not an active submission
interface. Minimum fields in a legacy entry:

```yaml
- rank: 1
  id: EC-V7-COST-059
  priority: P0
  status: queued
  readiness: ready            # ready | blocked | placeholder | deferred
  strategy: EmaConvictionTrendV7
  depends_on: ""              # optional dependency list / job id
  archetype: cost sensitivity
  hypothesis: ...
  expected: ...
  exchange: Binance Perpetual Futures
  pairs: [BTC-USDT, ETH-USDT]
  timeframes: [1h]
  leverage: 1x
  windows: [...]
  success_gate: ...
  failure_gate: ...
  next_step: ...
```

ATS Lab selects runnable work through its SQLite lifecycle gates. The retired
Jesse-side `queue_lifecycle.py` and Markdown `ready_queued` rule are no longer
operator interfaces. Placeholder strategy strings remain invalid for active
ATS work items.

## Status Values

- queued
- running
- blocked
- complete
- superseded

## Significant Result Memory Policy

Save memory only for durable research facts, for example:

- a strategy consistently failed a route family after robust tests
- a route/timeframe is repeatedly promising across multiple windows
- a Jesse config/tool quirk that affects future runs
- a stable research default or user preference

Do not save transient session IDs, one-off metrics, PR-style progress, or raw logs as memory.
