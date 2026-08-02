from __future__ import annotations

import unittest

from ats_lab.evidence import EvidenceSplit, NormalizedEvidence
from ats_lab.gates import evaluate_gates
from ats_lab.models import Verdict
from ats_lab.resources import ResourcePolicy


class GateTests(unittest.TestCase):
    def row(self, **overrides) -> NormalizedEvidence:
        payload = {
            "experiment_id": "EXP-1",
            "run_id": "RUN-1",
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


if __name__ == "__main__":
    unittest.main()
