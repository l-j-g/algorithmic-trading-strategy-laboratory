# Job synthesis

`ats-lab synthesize` turns one typed strategy idea into deterministic queue jobs.
Repeated calls are idempotent.

```bash
ats-lab synthesize --file idea.json
```

New or changed entry rules create:

```text
entry-significance (ready) -> baseline-backtest (scheduled)
```

The entry rule is normalized and SHA-256 fingerprinted. The fingerprint links
later revisions and permits reuse of an unchanged rule's evidence.

Gate behavior:

- `p_value < 0.05`: baseline becomes `ready`.
- `0.05 <= p_value <= 0.10`: baseline stays `scheduled` as inconclusive.
- `p_value > 0.10`: baseline becomes `archived`.
- no result: significance remains the executable job.
- `exit_only`, `sizing_only`, `risk_only`, or `refactor`: significance is skipped.

Example input:

```json
{
  "schema_version": 1,
  "strategy_name": "TrendPullback",
  "hypothesis": "Pullbacks inside a persistent trend continue.",
  "entry_rule": "EMA trend and RSI pullback reclaim",
  "change_scope": "new_entry",
  "priority": 20,
  "n_simulations": 2000,
  "random_seed": 42,
  "routes": [{
    "exchange": "Binance Perpetual Futures",
    "symbol": "BTC-USDT",
    "timeframe": "1h",
    "start_date": "2024-01-01",
    "finish_date": "2025-12-31"
  }]
}
```

Run synthesis again after the significance worker persists its normalized run
evidence. The engine reads `metrics.p_value` and reconciles the dependent job.

Synthesis does not invent routes, dates, strategy code, or results. Incomplete
ideas must remain scheduled until those inputs exist.

## Continuous replenishment

`ats-lab supervisor --continuous` asks its configured Agent profile for one typed
batch plan when unresolved chains reach the configured low watermark. Each cycle:

1. Acquires one transactional planner lease; concurrent workers cannot duplicate
   a cohort.
2. Inspects bounded improvement candidates and compact concept learnings.
   Total inspection records never exceed 25; each candidate contributes at most
   four canonical normalized evidence rows.
3. Prioritizes latest `revise`, then `inconclusive`.
4. Reserves at least five of 25 slots for new concepts and allows at most twenty
   controlled improvements, backfilling unused improvement slots with new ideas.
5. Excludes every experiment that has ever reached `hpo_candidate` or
   `paper_trade_candidate`.
6. Produces exactly 25 research chains. If Agent over-generates, laboratory
   deterministically trims extras while preserving five-new/twenty-improvement
   lane gates. Under-generation fails explicitly. A significance/baseline pair
   is one chain, not two.
7. Keeps at most configured batch capacity work items `ready` or `running`; overflow remains
   `scheduled` and is promoted as capacity opens.
8. Starts planning the next cohort at five unresolved chains, using all
   evaluations available at that point.

Each revision names its source experiment and one controlled change. Revision
depth is capped at three. Entry fingerprint remains stable for exit/sizing/risk
revisions; laboratory replaces any model-supplied entry text with source's
canonical entry rule for those scopes. Separate job fingerprint prevents
child-ID collisions.
Executor turn returns run evidence. Separate analyzer turn evaluates completed
batch. Separate synthesis turn replenishes only at low watermark, so SQLite
serves as both execution history and iterative learning repository. Significance
passes release their baseline locally; inconclusive or failed gates archive the
dependent baseline without another execution turn. Invalid synthesis output
fails its cohort and respects configured retry cooldown.
