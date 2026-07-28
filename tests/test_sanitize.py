import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import ExperimentSpec, RunResult, RunStatus, WorkItem, WorkState
from ats_lab.sanitize import apply_sanitize_plan, build_sanitize_plan


class SanitizeTests(unittest.TestCase):
    def test_deletes_dead_blocker_and_evaluates_finished_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            for item_id, state in (("DEAD", WorkState.BLOCKED), ("DONE", WorkState.FINISHED)):
                database.upsert_experiment(ExperimentSpec(id=item_id, strategy_name="Test"))
                database.upsert_work_item(WorkItem(
                    id=item_id, experiment_id=item_id, priority=20, state=state,
                    blocker_code="legacy_blocked" if item_id == "DEAD" else None,
                ))
            database.add_run(RunResult(
                id="run", experiment_id="DONE", work_item_id="DONE",
                session_id="session", status=RunStatus.FINISHED,
                metrics={"total_trades": 40, "expectancy": -1, "net_profit": -40, "max_drawdown": -5},
            ))
            plan = build_sanitize_plan(database)
            self.assertEqual(plan["counts"], {"delete": 1, "evaluate": 1})
            result = apply_sanitize_plan(database, plan)
            self.assertEqual(result["deleted"], ["DEAD"])
            self.assertEqual(result["evaluated"], ["DONE"])
            self.assertEqual(database.rows("SELECT verdict FROM evaluations")[0]["verdict"], "reject")
            self.assertEqual(database.rows("SELECT id FROM work_items"), [{"id": "DONE"}])
