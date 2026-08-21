from __future__ import annotations

import unittest

from ats_lab.evidence import (
    CostStressStatus,
    EvidenceSplit,
    LifecycleStage,
    NormalizedEvidence,
)
from ats_lab.gates import evaluate_gates, evaluate_hpo_candidate, evaluate_promotion
from ats_lab.models import Verdict
from ats_lab.resources import ResourcePolicy


class GateTests(unittest.TestCase):
    def row(self, **overrides) -> NormalizedEvidence:
        payload = {
            "experiment_id": "EXP-1",
            "run_id": "RUN-1",
            "session_id": "SESSION-1",
            "symbol": "BTC-USDT",
            "timeframe": "1h",
            "start_date": "2024-01-01",
            "finish_date": "2025-01-01",
            "net_profit_percentage": 12.0,
            "max_drawdown_percentage": 10.0,
            "sharpe_ratio": 1.2,
            "profit_factor": 1.4,
            "trade_count": 100,
            "fees": 20.0,
        }
        payload.update(overrides)
        return NormalizedEvidence(**payload)

    def test_all_numeric_and_route_gates_pass(self) -> None:
        decision = evaluate_gates(
            [self.row()],
            policy=ResourcePolicy(),
            expected_routes=[{
                "symbol": "BTC-USDT", "timeframe": "1h",
                "start_date": "2024-01-01", "finish_date": "2025-01-01",
            }],
        )
        self.assertEqual(decision.verdict, Verdict.PASS)
        self.assertEqual(decision.failed, ())

    def test_failed_profit_drawdown_and_trades_reject(self) -> None:
        decision = evaluate_gates(
            [self.row(
                net_profit_percentage=-1,
                max_drawdown_percentage=40,
                trade_count=10,
            )],
            policy=ResourcePolicy(),
        )
        self.assertEqual(decision.verdict, Verdict.REJECT)
        self.assertIn("net_profit", decision.failed)
        self.assertIn("max_drawdown", decision.failed)
        self.assertIn("minimum_trades", decision.failed)

    def test_trade_floor_scales_with_dated_window(self) -> None:
        decision = evaluate_gates(
            [self.row(trade_count=25)],
            policy=ResourcePolicy(),
        )
        self.assertNotIn("minimum_trades", decision.failed)

    def test_trade_floor_keeps_hard_floor_for_short_window(self) -> None:
        decision = evaluate_gates(
            [self.row(
                start_date="2026-01-01",
                finish_date="2026-03-01",
                trade_count=11,
            )],
            policy=ResourcePolicy(),
        )
        self.assertIn("minimum_trades", decision.failed)

    def test_train_holdout_degradation_uses_canonical_split(self) -> None:
        train = self.row(
            evidence_split=EvidenceSplit.TRAIN,
            net_profit_percentage=20,
        )
        holdout = self.row(
            evidence_split=EvidenceSplit.HOLDOUT,
            net_profit_percentage=5,
        )
        decision = evaluate_gates(
            [train, holdout],
            policy=ResourcePolicy(),
        )
        self.assertEqual(decision.holdout_degradation_percentage, 75.0)
        self.assertIn("train_holdout_degradation", decision.failed)

    def test_promotion_requires_unseen_window_and_cost_stress(self) -> None:
        decision = evaluate_promotion(
            [self.row()], policy=ResourcePolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(
            decision.missing,
            (
                "oos_validation", "walk_forward",
                "candles_based_monte_carlo_path_robustness",
                "walk_forward_protocol",
                "fees_cost_sensitivity",
            ),
        )

    def test_promotion_requires_both_validation_lanes_and_robustness(self) -> None:
        decision = evaluate_promotion(
            [
                self.row(
                    evidence_split=EvidenceSplit.OOS,
                    cost_stress_status=CostStressStatus.PASS,
                ),
            ],
            policy=ResourcePolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("walk_forward", decision.missing)
        self.assertIn("candles_based_monte_carlo_path_robustness", decision.missing)

    def test_promotion_passes_explicit_oos_walk_forward_mc_and_cost_stress(self) -> None:
        decision = evaluate_promotion(
            [
                self.row(
                    evidence_split=EvidenceSplit.OOS,
                    lifecycle_stage=LifecycleStage.OUT_OF_SAMPLE,
                    cost_stress_status=CostStressStatus.PASS,
                    sortino_ratio=1.0,
                    calmar_ratio=1.0,
                ),
                self.row(
                    evidence_split=EvidenceSplit.ROLLING,
                    lifecycle_stage=LifecycleStage.MULTI_WINDOW,
                    sortino_ratio=1.0,
                    calmar_ratio=1.0,
                    walk_forward_method="rolling",
                    walk_forward_windows=3,
                ),
                self.row(
                    lifecycle_stage=LifecycleStage.MONTE_CARLO,
                    calmar_ratio=1.1,
                    sortino_ratio=1.3,
                    monte_carlo_method="candle_based",
                    monte_carlo_scenarios=500,
                ),
            ],
            policy=ResourcePolicy(),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.failed, ())
        self.assertEqual(decision.missing, ())

    def test_promotion_rejects_monte_carlo_without_concrete_route_metrics(self) -> None:
        decision = evaluate_promotion(
            [
                self.row(evidence_split=EvidenceSplit.OOS, cost_stress_status=CostStressStatus.PASS),
                self.row(evidence_split=EvidenceSplit.ROLLING),
                self.row(
                    lifecycle_stage=LifecycleStage.MONTE_CARLO,
                    finding="all paths robust",
                    session_id=None,
                ),
            ],
            policy=ResourcePolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("candles_based_monte_carlo_path_robustness", decision.missing)

    def test_promotion_rejects_failed_oos_quality(self) -> None:
        decision = evaluate_promotion(
            [
                self.row(
                    evidence_split=EvidenceSplit.ROLLING,
                    net_profit_percentage=-1,
                    cost_stress_status=CostStressStatus.PASS,
                ),
            ],
            policy=ResourcePolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("net_profit", decision.failed)

    def hpo_family(self, **overrides) -> list[NormalizedEvidence]:
        payload = {
            "net_profit_percentage": 10.0, "trade_count": 100,
            "cost_stress_status": CostStressStatus.PASS,
        }
        payload.update(overrides)
        rows = []
        for symbol in ("BTC-USDT", "ETH-USDT"):
            for start, finish in (
                ("2024-01-01", "2024-07-01"), ("2024-07-01", "2025-01-01"),
            ):
                rows.append(self.row(
                    symbol=symbol, start_date=start, finish_date=finish,
                    **payload,
                ))
        return rows

    def test_hpo_candidate_passes_documented_criteria(self) -> None:
        decision = evaluate_hpo_candidate(
            self.hpo_family(), policy=ResourcePolicy(),
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.failed, ())
        self.assertEqual(decision.missing, ())

    def test_hpo_candidate_missing_evidence_is_never_allowed(self) -> None:
        decision = evaluate_hpo_candidate(
            [self.row()], policy=ResourcePolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.failed, ())
        self.assertIn("hpo_multi_window_positivity", decision.missing)
        self.assertIn("hpo_single_route_dominance", decision.missing)
        self.assertIn("fees_cost_sensitivity", decision.missing)

    def test_hpo_candidate_missing_fees_is_baseline_missing(self) -> None:
        decision = evaluate_hpo_candidate(
            [self.row(fees=None)], policy=ResourcePolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("hpo_baseline_positive_after_fees", decision.missing)

    def test_hpo_candidate_fails_activity_floor(self) -> None:
        decision = evaluate_hpo_candidate(
            self.hpo_family(trade_count=5), policy=ResourcePolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("hpo_activity_floor", decision.failed)

    def test_hpo_candidate_fails_single_dominant_route(self) -> None:
        rows = self.hpo_family()
        for index, row in enumerate(rows):
            rows[index] = self.row(**{
                **row.__dict__,
                "net_profit_percentage": 30.0 if row.symbol == "BTC-USDT" else -5.0,
            })
        decision = evaluate_hpo_candidate(rows, policy=ResourcePolicy())
        self.assertFalse(decision.allowed)
        self.assertIn("hpo_single_route_dominance", decision.failed)

    def test_hpo_candidate_fails_when_fee_stress_destroys_edge(self) -> None:
        decision = evaluate_hpo_candidate(
            self.hpo_family(cost_stress_status=CostStressStatus.FAIL),
            policy=ResourcePolicy(),
        )
        self.assertFalse(decision.allowed)
        self.assertIn("fees_cost_sensitivity", decision.failed)


if __name__ == "__main__":
    unittest.main()
