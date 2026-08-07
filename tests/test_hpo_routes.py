from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.hpo_routes import HpoRoutePlanner, default_hpo_routes
from ats_lab.models import Evaluation, ExperimentSpec, Verdict, WorkItem, WorkState


class HpoRoutePlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = WorkflowDatabase(Path(self.temp.name) / "lab.sqlite3")
        self.database.initialize()
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="Trend",
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1,
            state=WorkState.FINISHED,
        ))
        self.database.add_evaluation(Evaluation(
            experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plan_exposes_required_splits_and_safe_file_shape(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        plan = HpoRoutePlanner(self.database).build(study["id"])
        payload = plan.to_dict()

        self.assertEqual(payload["strategy"], "Trend")
        self.assertEqual(
            [split for split, item in payload["splits"].items() if item["ready"]],
            [],
        )
        self.assertEqual(
            payload["required_file_shape"], {"hpo": [], "oos": [], "rolling": []},
        )
        self.assertTrue(payload["warnings"][0].startswith("missing route splits"))
        self.assertIn("configure-hpo-validation-routes", payload["operator_command"])

    def test_plan_marks_configured_training_route_without_releasing_validation(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.configure_hpo_validation_routes(study["id"], {
            "hpo": [{
                "exchange": "Binance Perpetual Futures", "symbol": "BTC-USDT",
                "timeframe": "1h", "start_date": "2024-01-01",
                "finish_date": "2025-01-01",
            }],
        })
        plan = HpoRoutePlanner(self.database).build(study["id"]).to_dict()
        self.assertTrue(plan["splits"]["hpo"]["ready"])
        self.assertFalse(plan["splits"]["oos"]["ready"])
        self.assertFalse(plan["splits"]["rolling"]["ready"])

    def test_default_routes_are_disjoint_and_release_scheduled_study(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        routes = default_hpo_routes()
        self.assertEqual(routes["hpo"][0]["start_date"], "2024-01-01")
        self.assertEqual(routes["rolling"][0]["start_date"], "2025-01-01")
        self.assertEqual(routes["oos"][0]["start_date"], "2026-01-01")
        self.database.configure_default_hpo_routes(study["id"], routes)
        plan = HpoRoutePlanner(self.database).build(study["id"])
        self.assertTrue(all(item["ready"] for item in plan.splits.values()))
        work = self.database.rows(
            "SELECT state,blocker_code FROM work_items WHERE id=?",
            (study["hpo_work_item_id"],),
        )[0]
        self.assertEqual(work["state"], "scheduled")
        self.assertIsNone(work["blocker_code"])

    def test_default_routes_never_overwrite_partial_operator_routes(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.configure_hpo_validation_routes(study["id"], {
            "hpo": [{
                "exchange": "Binance Perpetual Futures", "symbol": "BTC-USDT",
                "timeframe": "1h", "start_date": "2020-01-01",
                "finish_date": "2021-01-01",
            }],
        })
        with self.assertRaises(ValueError):
            self.database.configure_default_hpo_routes(
                study["id"], default_hpo_routes(),
            )


if __name__ == "__main__":
    unittest.main()
