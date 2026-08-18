# Research Brief: Dynamic Leverage Allocation

Status: brief only. No runnable jobs are created by this document.

## Thesis

Dynamic leverage may improve risk-adjusted returns by increasing exposure in
favourable conditions and reducing exposure in hostile conditions. The main
risk is overfit sizing that increases tail loss without adding durable edge.

## Core question

Does a clean, causal dynamic leverage layer improve risk-adjusted performance
against fixed 1x, 2x, and 3x controls using the same entry logic, realistic
costs, and multiple market regimes?

## Separate research lanes

Run lanes independently. Each lane must preserve the unchanged-entry baseline
and fixed-leverage controls.

1. **Volatility targeting** — `target_volatility / realised_volatility`,
   smoothly scaled and hard-capped, default range 0.5x–3x.
2. **Signal-confidence scaling** — leverage from a causal confidence measure
   that is separable from entry generation.
3. **Regime-based scaling** — simple causal regime state, smoothly mapped to
   leverage; explicitly test chop and high-volatility failure regimes.
4. **Beta/relative-strength scaling** — secondary lane using rolling returns
   beta against BTC or a declared benchmark basket.

## Non-negotiable design rules

- Leverage decision uses information available at the decision candle only.
- Leverage layer stays separate from entry logic.
- Hard maximum leverage mandatory; do not HPO `L_max`.
- Prefer smooth scaling over binary switches.
- Always run fixed 1x, 2x, and 3x controls.
- Use realistic fees and funding assumptions where the harness supports them.
- Beta variants fail closed when the required BTC `data_route` is absent.
- Cross-mode liquidation claims require an account-level model. Use explicitly
  labelled isolated-margin jobs for liquidation-stress evidence until then.

## Success criteria

A dynamic rule must improve Sharpe, Sortino, or Calmar against the best fixed
control without materially worsening maximum drawdown or liquidation risk. The
finding must persist across train, unseen holdout, and rolling/walk-forward
regimes. A stronger aggregate return alone is insufficient.

## Required evidence

Every run must preserve the session snapshot and report both configured and
observed leverage:

- `configured_futures_leverage`
- `leverage_mode`
- `effective_leverage_mean`
- `effective_leverage_p95`
- `effective_leverage_max`
- `liquidation_count`

The executor must use per-session balance, fee, leverage, and leverage mode;
global exchange configuration must not be reread during a run. Compact polling
must return metrics, liquidation count, and status without shipping trades or
orders to an agent.

## Explicit non-goals

- No Kalman beta until a returns-beta scaler beats the fixed controls.
- No strategy-side exchange leverage mutation mid-bar.
- No fabricated funding, open-interest, or liquidation-heatmap feeds.
- No long-running jobs or automatic promotion from this brief.
