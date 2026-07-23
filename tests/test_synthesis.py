from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import RouteSpec, RunResult, RunStatus
from ats_lab.synthesis import SynthesisRequest, synthesize


ROUTE = RouteSpec(
    exchange="Binance Perpetual Futures", symbol="BTC-USDT", timeframe="1h",
    start_date="2024-01-01", finish_date="2025-12-31",
)


class SynthesisTests(unittest.TestCase):
    def make_request(self, scope: str = "new_entry") -> SynthesisRequest:
        return SynthesisRequest(
            strategy_name="TrendPullback", hypothesis="Trend pullbacks continue.",
            entry_rule="EMA trend AND RSI pullback reclaim", change_scope=scope,
            routes=(ROUTE,), random_seed=42,
        )

    def test_new_entry_creates_ready_significance_and_gated_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            result = synthesize(database, self.make_request())
            self.assertEqual(result["decision"], "awaiting_significance")
            states = {row["id"]: row["state"] for row in database.rows("SELECT id,state FROM work_items")}
            self.assertEqual(states[result["significance_job"]], "ready")
            self.assertEqual(states[result["baseline_job"]], "scheduled")
            significance = database.execution_request(result["significance_job"])
            self.assertEqual(significance["experiment"]["routes"], [ROUTE.__dict__])
            self.assertEqual(significance["work_item"]["parameters"]["n_simulations"], 2000)

    def test_passing_significance_unlocks_baseline_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            request = self.make_request()
            first = synthesize(database, request)
            database.add_run(RunResult(
                id="rst-run", experiment_id=first["significance_job"], work_item_id=first["significance_job"],
                session_id="rst-session", status=RunStatus.FINISHED, route=ROUTE,
                metrics={"p_value": 0.03, "n_simulations": 2000}, finished_at="2026-01-01T00:00:00Z",
            ))
            second = synthesize(database, request)
            third = synthesize(database, request)
            self.assertEqual(second["decision"], "significance_passed")
            self.assertEqual(second["baseline_state"], "ready")
            self.assertEqual(second, third)
            self.assertEqual(database.rows("SELECT COUNT(*) count FROM experiments")[0]["count"], 2)

    def test_failed_significance_archives_dependent_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            request = self.make_request()
            first = synthesize(database, request)
            database.add_run(RunResult(
                id="rst-run", experiment_id=first["significance_job"], work_item_id=first["significance_job"],
                session_id="rst-session", status=RunStatus.FINISHED, metrics={"p_value": 0.2},
            ))
            result = synthesize(database, request)
            self.assertEqual(result["decision"], "significance_failed")
            self.assertEqual(result["baseline_state"], "archived")

    def test_exit_only_change_skips_significance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            result = synthesize(database, self.make_request("exit_only"))
            self.assertIsNone(result["significance_job"])
            self.assertEqual(result["baseline_state"], "ready")
            self.assertEqual(database.rows("SELECT COUNT(*) count FROM experiments")[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
