from __future__ import annotations

import json
import math
import unittest

from ats_lab.evidence import (
    CandidateMetrics,
    CostStressStatus,
    EvidenceSplit,
    LifecycleStage,
    NormalizedEvidence,
    display_value,
    evidence_key,
    normalize_run_evidence,
)
from ats_lab.models import Verdict


class NormalizedEvidenceTests(unittest.TestCase):
    def test_candidate_metrics_is_exact_alias_and_serialization_is_stable(self) -> None:
        self.assertIs(CandidateMetrics, NormalizedEvidence)
        evidence = NormalizedEvidence(
            experiment_id="EXP-1",
            lifecycle_stage=LifecycleStage.HPO,
            evidence_split=EvidenceSplit.HOLDOUT,
            verdict=Verdict.HPO_CANDIDATE,
            net_profit_percentage=12.5,
        )

        payload = evidence.to_dict()

        self.assertEqual(list(payload), list(NormalizedEvidence.__dataclass_fields__))
        self.assertEqual(payload["lifecycle_stage"], "hpo")
        self.assertEqual(payload["evidence_split"], "holdout")
        self.assertIsNone(payload["sharpe_ratio"])
        compact = json.loads(evidence.to_compact_json())
        self.assertNotIn("sharpe_ratio", compact)
        self.assertEqual(compact["net_profit_percentage"], 12.5)

    def test_normalizes_atomic_routes_and_percentage_conventions(self) -> None:
        rows = normalize_run_evidence(
            experiment_id="EXP-1",
            run_id="RUN-1",
            session_id="parent",
            strategy="Trend",
            lifecycle_stage="baseline",
            experiment_spec={
                "strategy_version": "v2",
                "risk_per_trade_percentage": 1.5,
            },
            route={"exchange": "Binance", "timeframe": "1h"},
            metrics={
                "leverage": 3,
                "route_runs": [{
                    "session_id": "route-1",
                    "route": {
                        "symbol": "BTC-USDT",
                        "start_date": "2025-01-01",
                        "finish_date": "2025-12-31",
                        "evidence_split": "holdout",
                    },
                    "metrics": {
                        "net_profit_percentage": 12.5,
                        "max_drawdown": -7.25,
                        "win_rate": 0.42,
                        "total": 30,
                        "fee": 14.2,
                        "p_value": 0.04,
                    },
                }],
            },
            completed_at="2026-01-01T00:00:00Z",
        )

        self.assertEqual(len(rows), 1)
        evidence = rows[0]
        self.assertEqual(evidence.session_id, "route-1")
        self.assertEqual(evidence.strategy_version, "v2")
        self.assertEqual(evidence.symbol, "BTC-USDT")
        self.assertEqual(evidence.evidence_split, EvidenceSplit.HOLDOUT)
        self.assertEqual(evidence.net_profit_percentage, 12.5)
        self.assertEqual(evidence.max_drawdown_percentage, 7.25)
        self.assertEqual(evidence.win_rate, 42.0)
        self.assertEqual(evidence.trade_count, 30)
        self.assertEqual(evidence.fees, 14.2)
        self.assertEqual(evidence.leverage, 3.0)
        self.assertEqual(evidence.risk_per_trade_percentage, 1.5)
        self.assertEqual(evidence.significance_p_value, 0.04)

    def test_normalizes_leverage_contract_aliases_and_serializes_them(self) -> None:
        evidence = normalize_run_evidence(
            experiment_id="EXP-1", run_id="RUN-1", session_id="session",
            strategy="Trend", lifecycle_stage="baseline", experiment_spec={},
            route={}, metrics={
                "leverage_mode": "Cross Margin",
                "configured_leverage": 3,
                "mean_effective_leverage": 2.1,
                "effective_leverage_95th_percentile": 2.8,
                "max_effective_leverage": 3.0,
                "total_liquidations": 0,
            }, completed_at=None,
        )[0]

        self.assertIsNone(evidence.leverage)
        self.assertEqual(evidence.leverage_mode, "cross_margin")
        self.assertEqual(evidence.configured_futures_leverage, 3.0)
        self.assertEqual(evidence.effective_leverage_mean, 2.1)
        self.assertEqual(evidence.effective_leverage_p95, 2.8)
        self.assertEqual(evidence.effective_leverage_max, 3.0)
        self.assertEqual(evidence.liquidation_count, 0)
        full = evidence.to_dict()
        compact = json.loads(evidence.to_compact_json())
        for field in (
            "leverage_mode", "configured_futures_leverage",
            "effective_leverage_mean", "effective_leverage_p95",
            "effective_leverage_max", "liquidation_count",
        ):
            self.assertIn(field, full)
            self.assertEqual(compact[field], full[field])

    def test_leverage_fields_populate_only_from_their_own_sources(self) -> None:
        legacy = normalize_run_evidence(
            experiment_id="EXP-1", run_id="RUN-1", session_id="session",
            strategy="Trend", lifecycle_stage="baseline", experiment_spec={},
            route={}, metrics={"leverage": 2}, completed_at=None,
        )[0]
        configured = normalize_run_evidence(
            experiment_id="EXP-1", run_id="RUN-1", session_id="session",
            strategy="Trend", lifecycle_stage="baseline", experiment_spec={},
            route={}, metrics={"futures_leverage": 3}, completed_at=None,
        )[0]

        self.assertEqual(legacy.leverage, 2.0)
        self.assertIsNone(legacy.configured_futures_leverage)
        self.assertIsNone(configured.leverage)
        self.assertEqual(configured.configured_futures_leverage, 3.0)

    def test_route_rows_never_inherit_parent_outcome_aggregates(self) -> None:
        rows = normalize_run_evidence(
            experiment_id="EXP-1",
            run_id="RUN-1",
            session_id="parent",
            strategy="Trend",
            lifecycle_stage="baseline",
            experiment_spec={},
            route={"exchange": "Binance", "timeframe": "1h"},
            metrics={
                "net_profit_percentage": 99,
                "sharpe_ratio": 5,
                "win_rate": 0.9,
                "leverage": 3,
                "route_runs": [{
                    "session_id": "route-1",
                    "route": {
                        "symbol": "BTC-USDT",
                        "start_date": "2025-01-01",
                        "finish_date": "2025-12-31",
                    },
                    "metrics": {"total_trades": 30},
                }],
            },
            completed_at="2026-01-01T00:00:00Z",
        )

        self.assertEqual(len(rows), 1)
        evidence = rows[0]
        self.assertIsNone(evidence.net_profit_percentage)
        self.assertIsNone(evidence.sharpe_ratio)
        self.assertIsNone(evidence.win_rate)
        self.assertEqual(evidence.trade_count, 30)
        self.assertEqual(evidence.leverage, 3.0)

    def test_single_route_runs_keep_top_level_metrics(self) -> None:
        evidence = normalize_run_evidence(
            experiment_id="EXP-1", run_id="RUN-1", session_id="session",
            strategy="Trend", lifecycle_stage="baseline", experiment_spec={},
            route={}, metrics={"net_profit_percentage": 12.5},
            completed_at=None,
        )[0]

        self.assertEqual(evidence.net_profit_percentage, 12.5)

    def test_win_rate_units_are_declared_by_the_metric_key(self) -> None:
        fraction = normalize_run_evidence(
            experiment_id="EXP-1", run_id="RUN-1", session_id="session",
            strategy="Trend", lifecycle_stage="baseline", experiment_spec={},
            route={}, metrics={"win_rate": 0.42}, completed_at=None,
        )[0]
        percentage = normalize_run_evidence(
            experiment_id="EXP-1", run_id="RUN-1", session_id="session",
            strategy="Trend", lifecycle_stage="baseline", experiment_spec={},
            route={}, metrics={"win_rate_percentage": 42}, completed_at=None,
        )[0]
        violation = normalize_run_evidence(
            experiment_id="EXP-1", run_id="RUN-1", session_id="session",
            strategy="Trend", lifecycle_stage="baseline", experiment_spec={},
            route={}, metrics={"win_rate": 42}, completed_at=None,
        )[0]

        self.assertEqual(fraction.win_rate, 42.0)
        self.assertEqual(percentage.win_rate, 42.0)
        self.assertIsNone(violation.win_rate)

    def test_currency_net_profit_never_becomes_percentage(self) -> None:
        evidence = normalize_run_evidence(
            experiment_id="EXP-1",
            run_id="RUN-1",
            session_id="session",
            strategy="Trend",
            lifecycle_stage="baseline",
            experiment_spec={},
            route={},
            metrics={"net_profit": 1000, "win_rate_percentage": 48},
            completed_at=None,
        )[0]

        self.assertIsNone(evidence.net_profit_percentage)
        self.assertEqual(evidence.win_rate, 48)

    def test_normalizes_typed_monte_carlo_and_walk_forward_protocol(self) -> None:
        monte_carlo = normalize_run_evidence(
            experiment_id="EXP-1", run_id="MC-1", session_id="mc-session",
            strategy="Trend", lifecycle_stage="monte_carlo", experiment_spec={},
            route={"symbol": "BTC-USDT", "timeframe": "1h",
                   "start_date": "2025-01-01", "finish_date": "2025-06-01"},
            metrics={"monte_carlo_method": "candles-based",
                     "monte_carlo_scenarios": 500}, completed_at=None,
        )[0]
        rolling = normalize_run_evidence(
            experiment_id="EXP-1", run_id="WF-1", session_id="wf-session",
            strategy="Trend", lifecycle_stage="out_of_sample", experiment_spec={},
            route={"symbol": "BTC-USDT", "timeframe": "1h",
                   "start_date": "2025-06-01", "finish_date": "2025-08-01",
                   "evidence_split": "rolling"},
            metrics={"walk_forward_method": "rolling", "walk_forward_windows": 4},
            completed_at=None,
        )[0]
        self.assertEqual(monte_carlo.monte_carlo_method, "candle_based")
        self.assertEqual(monte_carlo.monte_carlo_scenarios, 500)
        self.assertEqual(rolling.walk_forward_method, "rolling")
        self.assertEqual(rolling.walk_forward_windows, 4)

    def test_derives_profit_factor_from_raw_jesse_gross_totals(self) -> None:
        evidence = normalize_run_evidence(
            experiment_id="EXP-1",
            run_id="RUN-1",
            session_id="session",
            strategy="Trend",
            lifecycle_stage="baseline",
            experiment_spec={},
            route={},
            metrics={"gross_profit": 250.0, "gross_loss": -200.0},
            completed_at=None,
        )[0]

        self.assertEqual(evidence.profit_factor, 1.25)

    def test_invalid_values_are_null_and_oos_split_is_deterministic(self) -> None:
        evidence = normalize_run_evidence(
            experiment_id="EXP-1",
            run_id="RUN-1",
            session_id=None,
            strategy="Trend",
            lifecycle_stage="out_of_sample",
            experiment_spec={},
            route={},
            metrics={
                "sharpe_ratio": math.inf,
                "trade_count": -1,
                "cost_stress_status": "passed",
            },
            completed_at="",
        )[0]

        self.assertIsNone(evidence.sharpe_ratio)
        self.assertIsNone(evidence.trade_count)
        self.assertEqual(evidence.evidence_split, EvidenceSplit.OOS)
        self.assertIsNone(evidence.cost_stress_status)
        self.assertIsNone(evidence.completed_at)

    def test_self_reported_cost_stress_status_is_not_persisted(self) -> None:
        evidence = normalize_run_evidence(
            experiment_id="EXP-1",
            run_id="RUN-1",
            session_id=None,
            strategy="Trend",
            lifecycle_stage="baseline",
            experiment_spec={},
            route={},
            metrics={"cost_stress_status": "passed"},
            completed_at=None,
        )[0]

        self.assertIsNone(evidence.cost_stress_status)

    def test_machine_cost_stress_rows_persist_their_status(self) -> None:
        evidence = normalize_run_evidence(
            experiment_id="EXP-1-COST2X",
            run_id="RUN-2",
            session_id=None,
            strategy="Trend",
            lifecycle_stage="cost_sensitivity",
            experiment_spec={"fee_rate": 0.001},
            route={},
            metrics={"cost_stress_status": "failed"},
            completed_at=None,
        )[0]

        self.assertEqual(evidence.cost_stress_status, CostStressStatus.FAIL)

    def test_monte_carlo_tail_summaries_are_normalized(self) -> None:
        evidence = normalize_run_evidence(
            experiment_id="EXP-1",
            run_id="RUN-3",
            session_id=None,
            strategy="Trend",
            lifecycle_stage="paper_trade",
            experiment_spec={},
            route={},
            metrics={
                "net_profit_percentage": 18.0,
                "monte_carlo_best_5pct_net_profit_percentage": 32.5,
                "monte_carlo_worst_5pct_net_profit_percentage": -4.5,
            },
            completed_at=None,
        )[0]
        sparse = normalize_run_evidence(
            experiment_id="EXP-1",
            run_id="RUN-4",
            session_id=None,
            strategy="Trend",
            lifecycle_stage="paper_trade",
            experiment_spec={},
            route={},
            metrics={"net_profit_percentage": 18.0},
            completed_at=None,
        )[0]

        self.assertEqual(
            evidence.monte_carlo_best_5pct_net_profit_percentage, 32.5,
        )
        self.assertEqual(
            evidence.monte_carlo_worst_5pct_net_profit_percentage, -4.5,
        )
        self.assertIsNone(sparse.monte_carlo_best_5pct_net_profit_percentage)
        self.assertIsNone(sparse.monte_carlo_worst_5pct_net_profit_percentage)

    def test_key_is_deterministic_and_display_preserves_zero(self) -> None:
        evidence = NormalizedEvidence(
            experiment_id="EXP-1",
            run_id="RUN-1",
            session_id="session",
            symbol="BTC-USDT",
        )

        self.assertEqual(evidence_key(evidence), evidence_key(evidence))
        self.assertEqual(display_value(None), "—")
        self.assertEqual(display_value(0), "0")


if __name__ == "__main__":
    unittest.main()
