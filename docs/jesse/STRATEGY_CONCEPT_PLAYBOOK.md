# Jesse Strategy Concept Playbook

Purpose: a conceptual library for thesis-led strategy research. The agent should consult this before proposing enhancements, variants, or new queue jobs.

Scope: research/backtesting only. This is not live-trading guidance and must not be used to configure real execution.

## How To Use This Playbook

Use this document as a design menu, not a recipe book.

For every strategy idea or refinement:

1. Start with the market thesis.
2. Pick one archetype.
3. Pick one target regime and one failure regime.
4. Select the minimum indicator/filter set needed to express the thesis.
5. Pick one entry trigger, one exit framework, and one sizing/risk model.
6. Define what would falsify the idea before running the backtest.
7. Add exactly one controlled refinement at a time.

Default Jesse research preference: one regime filter, one entry trigger, one exit framework. Avoid indicator stacking unless each component has a clear job.

## Strategy Archetypes

| Archetype | Core Thesis | Target Regime | Failure Regime | Typical Tools | Key Risk |
|---|---|---|---|---|---|
| Trend following | Crypto trends persist after directional confirmation. | Sustained directional movement, high ADX, MA slope. | Sideways chop, fake breakouts. | MA/KAMA/EMA, Donchian, SuperTrend, ADX. | Whipsaw and late exits. |
| Trend pullback | In strong trends, pullbacks revert back toward trend direction. | Directional trend with temporary mean reversion. | Deep trend reversal or range. | HTF MA filter, CCI/RSI/Williams, ATR stop. | Buying pullbacks that become reversals. |
| Breakout | Range compression resolves into directional expansion. | Low-to-high volatility transition. | Failed breakout/range snapback. | Donchian, range high/low, ATR expansion, volume. | False breakouts and slippage. |
| Failed breakout fade | Failed extremes revert after liquidity sweep. | Range/chop with weak follow-through. | Strong trend continuation. | Prior high/low, Bollinger/Keltner, time stop. | Fighting real breakouts. |
| Mean reversion | Short-term overextension mean reverts. | Sideways range, volatility bounded. | Trend expansion. | RSI, Bollinger Bands, z-score, VWAP bands. | Catching falling knives / shorting squeezes. |
| Volatility expansion | Compression precedes outsized moves. | Squeeze then expansion. | Persistent low-vol drift or noisy chop. | ATR percentile, Bollinger width, Keltner squeeze. | Entering before direction is known. |
| Exhaustion reversal | Climactic moves mean revert after momentum exhaustion. | Blowoff/capitulation, divergence. | Persistent momentum. | RSI/Stoch divergence, volume spike, ATR blowout. | Early reversal entries. |
| Carry/relative strength research | Strong assets outperform weak assets. | Cross-sectional dispersion. | Correlation spikes. | Relative momentum, BTC/ETH/SOL spread filters. | Route overfit and regime dependence. |

## Regime Filters

Use regime filters to decide when the strategy is allowed to trade. They should answer: “Is this environment suitable for my edge?”

| Filter | Best For | Use When | Avoid When | Common Jesse Form |
|---|---|---|---|---|
| MA slope/alignment | Trend following, pullbacks. | Need simple direction bias. | Mean reversion in ranges. | price > SMA/KAMA and MA slope > 0. |
| Higher-timeframe trend | Pullbacks, breakouts. | Entry timeframe is noisy. | Very short scalps needing speed. | 1h entries gated by 4h trend. |
| ADX / trend strength | Trend pullbacks, breakouts. | Want to avoid low-trend chop. | Early trend detection; ADX lags. | ADX > threshold and DI alignment. |
| ATR percentile / volatility | Breakouts, stop sizing. | Need volatility-aware risk. | Thin data or unstable vol regimes. | ATR / price or rolling ATR rank. |
| Bollinger width / squeeze | Vol expansion. | Looking for compression. | Already-trending markets. | Band width below percentile, then expansion. |
| Range/chop filter | Mean reversion/fades. | Want bounded markets. | Strong trends. | low ADX, flat MA, price inside bands. |
| Session/time filter | Intraday systems. | Market microstructure matters. | 4h/daily crypto research unless proven. | Restrict hour/day. |
| Route filter | Candidate hardening. | BTC works but ETH/SOL fail. | Claiming universal edge. | Explicit pair-specific job queue route. |

Guideline: filter for the failure regime, not for beauty. Example: if ETH chop kills a trend system, test ADX/HTF slope filters against ETH chop windows.

## Indicator Families And When To Use Them

### Moving Averages

Use for trend direction, slope, and dynamic support/resistance.

- SMA: simple, slow, good control baseline.
- EMA/TEMA: faster response, higher whipsaw risk.
- KAMA: adaptive trend/pullback research; useful when chop is a problem.
- VWAP/VWMA: volume-weighted mean; more useful intraday or on volume-sensitive ideas.

Pitfall: MA crossovers often lag. Use them as regime context more often than as the only entry trigger.

### Momentum Oscillators

Use for pullback, overextension, or confirmation.

- RSI: general overbought/oversold and momentum state.
- CCI: pullback/mean deviation; useful for TrendWave-like systems.
- Williams %R / Stochastic: faster pullback timing, more noise.
- MACD: broad momentum confirmation; laggy but interpretable.

Pitfall: oscillator oversold is bullish only in a suitable regime. In a downtrend it can stay oversold.

### Volatility And Range

Use for stops, targets, compression, and regime classification.

- ATR: default stop/target distance and volatility normalization.
- Bollinger Bands: mean reversion and volatility expansion.
- Keltner Channels: smoother ATR-based channel; useful with squeeze logic.
- Donchian Channels: breakout and trend systems.

Pitfall: fixed ATR multipliers can overfit. Test nearby values and cost sensitivity before trusting.

### Trend Strength

Use to separate directional from range behavior.

- ADX: trend strength; useful to suppress chop, but lagging.
- DI+/DI-: direction confirmation.
- SuperTrend: trend state plus trailing stop idea; can whipsaw.
- Ichimoku: broad regime context, often sparse/lagging.

Pitfall: high ADX can occur late in a move. Pair it with pullback/reclaim logic rather than blindly chasing.

### Volume / Participation

Use only when the thesis needs participation confirmation.

- Volume spike: breakout confirmation or exhaustion.
- OBV/CMF: accumulation/distribution hypothesis.
- VWAP bands: intraday mean reversion or institutional mean.

Pitfall: crypto exchange volume differs by venue; avoid making volume mandatory without route-specific evidence.

### Statistical Normalization

Use when comparing conditions across volatile regimes.

- z-score: overextension relative to recent distribution.
- rolling percentile/rank: robust filter thresholding.
- correlation/beta: market-relative research.

Pitfall: statistical filters can hide lookback overfit. Validate across multiple windows.

## Multi-Timeframe Analysis

Use MTF when the entry timeframe is noisy but the thesis depends on a larger structure.

Common patterns:

1. 4h trend, 1h pullback entry.
2. 1D trend, 4h breakout entry.
3. 4h range/chop filter, 1h mean reversion entry.

Use MTF for:

- trend confirmation
- failure-regime filtering
- trade direction bias
- avoiding low-quality countertrend entries

Avoid MTF when:

- it creates too few trades
- higher timeframe lags so much that entries are late
- it merely stacks confirmations without a falsifiable reason

Research rule: compare single-timeframe baseline vs MTF variant. MTF must improve expectancy, drawdown, or route robustness, not just make the chart look cleaner.

## Entry Trigger Patterns

| Trigger | Best For | Confirmation Needed | Failure Mode |
|---|---|---|---|
| Pullback threshold | Trend pullback. | Trend regime filter. | Pullback becomes reversal. |
| Reclaim after pullback | Trend pullback. | Close back above MA/band/level. | Late entry after bounce. |
| Breakout close | Breakout/trend. | Vol expansion or HTF trend. | False breakout. |
| Breakout + retest | Breakout pullback. | Level hold. | Missed runaway moves. |
| Band touch + revert | Mean reversion. | Range regime. | Trend walks the band. |
| Divergence/exhaustion | Reversal. | Vol/volume climax. | Early countertrend entry. |
| Time-based exit trigger | Scalp/fade. | Short horizon edge. | Exits before real move. |

## Exit Frameworks

| Exit | Use When | Pros | Cons |
|---|---|---|---|
| ATR stop + fixed R target | Baseline research. | Simple and comparable. | Can truncate trends. |
| Trailing ATR stop | Trend following. | Captures larger moves. | Gives back open profit. |
| MA/KAMA close exit | Trend systems. | Aligns with regime thesis. | Late during fast reversals. |
| Time stop | Failed breakout/scalp/mean reversion. | Controls dead trades. | May exit before delayed edge. |
| Partial TP + runner | Trend pullbacks/breakouts. | Balances hit rate and convexity. | More parameters; harder to interpret. |
| Opposite signal exit | Oscillator/mean reversion. | Symmetric logic. | Can hold losers too long. |
| Volatility contraction exit | Breakouts. | Exits when expansion fails. | Requires careful definition. |

Research rule: change exits one at a time. For example, do not add trailing stop and sizing changes in the same variant.

## Position Sizing And Risk Management

Default research goal is signal validation, not maximizing leverage.

| Technique | Use When | Benefit | Risk / Caveat |
|---|---|---|---|
| Fixed notional / fixed fraction | Baseline comparisons. | Simple, comparable. | Ignores volatility. |
| ATR volatility sizing | Vol differs by asset/window. | Normalizes risk. | Can oversize quiet regimes before expansion. |
| Fixed percent risk per trade | Most backtest baselines. | Drawdown interpretable. | Stop distance errors distort size. |
| Drawdown throttle | Candidate hardening. | Reduces ruin risk. | Can suppress recovery. |
| Pair-specific sizing | Route-specific candidates. | Acknowledges BTC/ETH/SOL differences. | Risk of route overfit. |
| Leverage stress grid | Post-baseline only. | Tests liquidation/drawdown sensitivity. | Can turn modest edge into unacceptable risk. |

Defaults for this research workflow:

- Start with 1x.
- Try 2x only after 1x robustness and drawdown review.
- Treat 3x as stress testing.
- Avoid 5x unless specifically reviewing liquidation buffer after strong evidence.
- Do not use leverage to rescue weak expectancy.

## Cross-Pair Research Notes

### ETH-BTC

- Treat ETH-BTC as an ETH/BTC relative-strength chart, not a USD beta route. Its edge thesis should be about ETH outperforming or underperforming BTC, not generic crypto trend beta.
- Start with range/mean-reversion or failed-breakout-fade baselines before trend pullback. The pair often trades in long bounded regimes where band touches, z-scores, or failed extremes are more falsifiable than momentum continuation.
- Target regime: flat or gently mean-reverting ETH/BTC range, low-to-moderate ADX, price reverting from Bollinger/VWAP/z-score extremes.
- Failure regime: structural ETH/BTC repricing trend where price walks the band and countertrend entries compound losses.
- Use Binance Spot ETH-BTC data first. Keep 1x research sizing and evaluate returns in BTC terms; do not compare raw PnL directly to USDT-perp routes without labeling the quote-currency difference.
- Baseline gate: require multi-window persistence and bounded drawdown before HPO/Monte Carlo.

## Enhancement Decision Tree

When improving a strategy, choose the enhancement based on the observed weakness:

| Observed Weakness | First Enhancement To Test | Why |
|---|---|---|
| Good bull returns, chop losses | ADX/HTF slope/range filter | Directly targets failure regime. |
| Good route, too high DD | Lower risk per trade or tighter stop | Risk first; do not add indicators. |
| Good PF, too few trades | Lower timeframe or less strict trigger | Tests signal density. |
| Many small wins, occasional huge loss | Stop/time stop/regime kill switch | Tail-risk problem. |
| Low win rate but large winners | Trailing stop / runner logic | Preserve convexity. |
| High win rate, poor expectancy | Larger target or avoid low-R trades | Winners too small. |
| BTC works, ETH fails | Route-specific hypothesis or ETH chop veto | Do not claim broad edge. |
| Turns negative under 2x fees | Reject or reduce turnover | Cost sensitivity is fatal. |
| Only one window works | Multi-window validation before HPO | Avoid 2024-only overfit. |

## Queue Generation Rules

New jobs should be generated from evidence, not curiosity alone.

Good follow-up job types:

- Baseline route sweep.
- Multi-window validation.
- Regime-window split.
- Fee/slippage sensitivity.
- Single controlled enhancement.
- Route-specific validation.
- Monte Carlo/path robustness after candidate evidence.
- Rule significance test for candidate entry signal.
- HPO only after baseline gates pass.

Bad follow-up job types:

- Add three filters at once.
- HPO a losing baseline.
- Increase leverage to make returns exciting.
- Re-test only the best window.
- Compare private/untracked source without recording enough reproducible context.

## Monte Carlo Robustness

Use Jesse MCP Monte Carlo as a candidate-hardening tool, not as a replacement for baseline route/window testing.

Best use cases:

- A baseline or route-specific candidate already has positive expectancy after fees.
- Multi-window/OOS behavior is not catastrophic.
- Cost sensitivity is tolerable.
- Trade count is large enough that path variation is meaningful.
- We need to know whether the original equity curve is representative or lucky.

Preferred MCP setup:

- `run_candles = true` as the default. Candle resampling is more informative because it perturbs the underlying price path and re-runs the strategy.
- `run_trades = false` initially. Trade-order resampling is fast but lower-signal; add it only when explicitly studying trade-sequence fragility.
- `num_scenarios = 200` for routine candidate checks.
- `num_scenarios = 500-1000` only for final tail confidence after the candidate already looks strong.
- `pipeline_type = moving_block_bootstrap` unless there is a specific reason to test another resampling model.

Interpretation rule:

- For higher-is-better metrics such as net profit, Sharpe, Calmar, win rate, and profit factor, the original should be near the Monte Carlo median or moderately above it.
- If original is above the best 5% tail, treat the result as suspicious/overfit: the original run may be luckier than almost all resampled paths.
- If original is below the median but still profitable/controlled, that can be robust: the original path was not especially lucky.
- Always inspect the worst 5% downside. A candidate can have good median behavior but still be rejected if worst-tail drawdown or loss is unacceptable.

Monte Carlo should generate one of these follow-up outcomes:

- `hpo-candidate`: original is plausible vs MC distribution and worst-tail risk is acceptable.
- `revise`: downside tail is too weak, but the baseline edge remains plausible; test sizing/stop/regime hardening.
- `reject`: original looks like a lucky tail or most scenarios fail after fees.

Do not use Monte Carlo to rescue a losing baseline. If baseline expectancy is negative after fees, fix or reject the thesis first.

## Candidate Gates

A strategy may move toward `hpo-candidate` only after:

- baseline route evidence is positive or clearly route-specific
- drawdown is acceptable for the route/timeframe
- trade count is not too sparse for the claim
- fee/slippage sensitivity is tolerable
- multi-window or OOS behavior is not catastrophic
- parameter sensitivity is plausible, not cliff-like
- Monte Carlo robustness is acceptable for any serious candidate claim

A strategy may move toward `paper-trade candidate` only after stronger validation, including OOS, cost sensitivity, Monte Carlo/path robustness, and rule-significance checks when appropriate. This is still not a live-trading recommendation.

## Anti-Patterns

- Indicator stacking without an explicit job for each indicator.
- Optimizing before understanding failure regimes.
- Treating ETH/SOL failure as irrelevant while claiming broad crypto edge.
- Ignoring fees on 1h/scalp systems.
- Using high leverage before liquidation/drawdown review.
- Treating one profitable backtest as proof.
- Changing entry, exit, sizing, and filters at once.

## Harness Safety Patterns

These patterns address recurring setup failures observed in Jesse research. Apply them proactively when creating new strategy variants or queue jobs.

### H1: Inherited Order Sizing Trap

**Problem:** Strategies that inherit from a parent using `risk_to_qty(self.available_margin, 3, ...)` then multiply (`qty*3`) submit orders at ~9% of margin per trade. At 1x leverage with $10k margin, this produces $20k-$30k notional orders that trigger `InsufficientMargin`.

**Seen in:** KAMA_TrendFollowing, KamaPullbackReclaim, TemaTrendFollowing.

**Fix pattern:**
- Replace `qty*3` with `qty*1` (or remove the multiplier entirely)
- Or cap risk at 1%: `risk_to_qty(self.available_margin, 1, ...)`
- Validate on a single atomic route before fanning out

**Queue convention:** When creating a harness-fix job, use the naming pattern `STRATEGY-FIX-NNN` and scope it to a single route (e.g., BTC 4h 2024). Only fan out after the fix is confirmed.

### H2: Missing Higher-Timeframe Data Routes

**Problem:** Strategies that call `self.get_candles(self.exchange, self.symbol, '4h')` or `'6h'` inside indicator properties require corresponding `data_routes` in the Jesse backtest draft. Without them, the HTF candles are empty and the strategy either stops or produces zero trades.

**Seen in:** KAMA_TrendFollowing (needs 6h for 4h routes), TemaTrendFollowing (needs 4h for 1h routes), KamaPullbackReclaim (inherits KAMA_TrendFollowing's needs).

**Fix pattern:**
- For 1h routes with HTF filter: add `{"exchange": "...", "symbol": "...", "timeframe": "4h"}` to `data_routes`
- For 4h routes with HTF filter: add `{"exchange": "...", "symbol": "...", "timeframe": "6h"}` to `data_routes`
- Verify candle coverage exists for the HTF before running

**Queue convention:** When a multi-TF strategy fails with zero trades or missing metrics, check data_routes before concluding strategy failure.

### H3: Wrong Exchange Config

**Problem:** Backtest drafts for Binance Spot pairs (e.g., ETH-BTC) fail silently when the backtest config only lists Binance Perpetual Futures. Sessions stay `draft` with `executing=false`.

**Seen in:** ETHBTC-RG-051, ETHBTC-RG-MW-052.

**Fix pattern:**
- Add the required exchange to backtest config before running Spot jobs
- Binance Spot config: `{"name": "Binance Spot", "fee": 0.001, "balance": 5000, "type": "spot"}`
- Verify with `get_backtest_config()` before launching sessions

### H4: Multi-TF Route Splitting

**Problem:** Jesse routes cannot include the same exchange-symbol pair more than once in one session, even with different timeframes. Multi-TF sweeps must be split into separate atomic backtests.

**Fix pattern:** One route per exchange-symbol-TF combination. Use `data_routes` for non-trading HTF data. Fan out independent atomic sessions in parallel.

## Agent Checklist Before Suggesting A Refinement

- What exact weakness am I addressing?
- Which failure regime caused it?
- What is the simplest concept from this playbook that targets that weakness?
- What single-code or single-config change tests it?
- What metric should improve if the idea is right?
- What result would make me reject it?
- Is this a baseline, robustness test, or candidate-hardening step?
- **Have I checked for inherited sizing bugs (H1)?**
- **Have I verified HTF data_routes exist for multi-TF strategies (H2)?**
- **Have I confirmed the exchange config matches the route's exchange (H3)?**
