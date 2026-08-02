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
        self.assertEqual(evidence.cost_stress_status, CostStressStatus.PASS)
        self.assertIsNone(evidence.completed_at)

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
