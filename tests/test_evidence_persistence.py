from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ats_lab import SCHEMA_VERSION
from ats_lab.database import WorkflowDatabase
from ats_lab.evidence import EvidenceSplit
from ats_lab.models import (
    Evaluation,
    ExperimentSpec,
    ExperimentType,
    RouteSpec,
    RunResult,
    RunStatus,
    Verdict,
    WorkItem,
    WorkState,
)


class EvidencePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = WorkflowDatabase(Path(self.temp.name) / "lab.sqlite3")
        self.database.initialize()
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-1",
            strategy_name="Trend",
            experiment_type=ExperimentType.BASELINE,
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-1",
            experiment_id="EXP-1",
            priority=1,
            state=WorkState.RUNNING,
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _add_atomic_run(self) -> None:
        self.database.add_run(RunResult(
            id="RUN-1",
            experiment_id="EXP-1",
            work_item_id="JOB-1",
            session_id="parent-session",
            status=RunStatus.FINISHED,
            route=RouteSpec(
                exchange="Binance",
                symbol="BTC-USDT",
                timeframe="1h",
                start_date="2025-01-01",
                finish_date="2025-12-31",
            ),
            metrics={
                "route_runs": [
                    {
                        "session_id": "route-1",
                        "route": {"evidence_split": "train"},
                        "metrics": {
                            "net_profit_percentage": 10,
                            "total_trades": 40,
                        },
                    },
                    {
                        "session_id": "route-2",
                        "route": {"evidence_split": "holdout"},
                        "metrics": {
                            "net_profit_percentage": 4,
                            "total_trades": 20,
                        },
                    },
                ],
            },
            raw_result={
                "session_id": "parent-session",
                "status": "finished",
                "metrics": {
                    "route_runs": [{"session_id": "route-1"}],
                },
            },
            finished_at="2026-01-01T00:00:00Z",
        ))

    def test_schema_v4_and_run_write_persist_atomic_routes(self) -> None:
        self._add_atomic_run()

        rows = self.database.normalized_evidence_for_run("RUN-1")

        self.assertEqual(SCHEMA_VERSION, 4)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row.evidence_split for row in rows},
            {EvidenceSplit.TRAIN, EvidenceSplit.HOLDOUT},
        )
        self.assertEqual({row.strategy for row in rows}, {"Trend"})
        self.assertEqual(
            self.database.rows(
                "SELECT version FROM schema_migrations ORDER BY version"
            )[-1]["version"],
            4,
        )

    def test_evaluation_enriches_every_atomic_row(self) -> None:
        self._add_atomic_run()

        self.database.add_evaluation(Evaluation(
            experiment_id="EXP-1",
            verdict=Verdict.HPO_CANDIDATE,
            summary="Holdout remained profitable.",
            next_step="Run HPO.",
            evaluator="test",
            evaluated_at="2026-01-01T01:00:00Z",
        ))

        rows = self.database.normalized_evidence_for_run("RUN-1")
        self.assertEqual({row.verdict for row in rows}, {Verdict.HPO_CANDIDATE})
        self.assertEqual(
            {row.finding for row in rows}, {"Holdout remained profitable."},
        )
        self.assertEqual({row.next_action for row in rows}, {"Run HPO."})

    def test_finalize_evaluation_and_work_item_are_atomic(self) -> None:
        self._add_atomic_run()
        self.database.mark_awaiting_evaluation("JOB-1", "BATCH-1")

        item = self.database.finalize_batch_evaluation(Evaluation(
            experiment_id="EXP-1",
            verdict=Verdict.REVISE,
            summary="Drawdown too high.",
            next_step="Reduce risk.",
            evaluator="batch",
        ))

        self.assertEqual(item["state"], "finished")
        evidence = self.database.normalized_evidence_for_run("RUN-1")
        self.assertEqual({row.verdict for row in evidence}, {Verdict.REVISE})
        self.assertEqual({row.finding for row in evidence}, {"Drawdown too high."})

    def test_compatible_query_requires_exact_tuple(self) -> None:
        self._add_atomic_run()
        anchor = self.database.query_normalized_evidence({
            "run_id": "RUN-1",
            "evidence_split": EvidenceSplit.TRAIN,
        })[0]

        compatible = self.database.compatible_evidence(anchor)

        self.assertEqual(len(compatible), 1)
        self.assertEqual(compatible[0].evidence_split, EvidenceSplit.TRAIN)
        with self.assertRaises(ValueError):
            self.database.query_normalized_evidence({"raw_metrics": "bad"})

    def test_backfill_is_idempotent_and_raw_is_diagnostic_only(self) -> None:
        self._add_atomic_run()
        self.database.rows("DELETE FROM normalized_evidence")

        first = self.database.backfill_normalized_evidence()
        second = self.database.backfill_normalized_evidence()
        diagnostic = self.database.diagnostic_raw_evidence("RUN-1")

        self.assertEqual(first["inserted"], 2)
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(diagnostic["metrics"]["route_runs"][0]["session_id"], "route-1")
        self.assertEqual(
            diagnostic["raw_result"]["session_id"],
            "parent-session",
        )
        normal = self.database.normalized_evidence_for_run("RUN-1")[0].to_dict()
        self.assertNotIn("metrics_json", normal)
        self.assertNotIn("route_json", normal)
        events = self.database.rows(
            """SELECT payload_json FROM events
               WHERE event_type='evidence_backfilled'"""
        )
        self.assertEqual(json.loads(events[-1]["payload_json"])["scanned"], 1)

    def test_raw_diagnostic_missing_run_returns_none(self) -> None:
        self.assertIsNone(self.database.diagnostic_raw_evidence("missing"))


if __name__ == "__main__":
    unittest.main()
