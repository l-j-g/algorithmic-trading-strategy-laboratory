from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ats_lab.database import WorkflowDatabase
from ats_lab.models import ExperimentSpec, WorkItem, WorkState
from ats_lab.status import hpo_lifecycle_snapshot, operator_status


class OperatorStatusTests(unittest.TestCase):
    def test_hpo_snapshot_uses_shared_lifecycle_and_analyzer_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            with (
                patch.object(WorkflowDatabase, "hpo_studies", return_value=[
                    {"lifecycle_state": "hpo_running"},
                    {"lifecycle_state": "validation"},
                    {"lifecycle_state": "reject"},
                ]),
                patch.object(
                    WorkflowDatabase, "current_analyzer_status",
                    return_value={"job_id": "ANALYZE-1", "state": "running"},
                ),
                patch.object(
                    WorkflowDatabase, "work_item_stage_timings",
                    return_value=[{
                        "stage": "hpo_analysis", "duration_seconds": 10,
                    }],
                ),
            ):
                snapshot = hpo_lifecycle_snapshot(database)

            self.assertEqual(snapshot["active"], 2)
            self.assertEqual(snapshot["counts"]["hpo_running"], 1)
            self.assertEqual(snapshot["counts"]["validation"], 1)
            self.assertEqual(snapshot["counts"]["reject"], 1)
            self.assertEqual(snapshot["analyzer"]["state"], "running")

    def test_null_blocker_running_claim_is_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="TestStrategy",
            ))
            database.upsert_work_item(WorkItem(
                id="JOB-1", experiment_id="EXP-1", priority=1,
                state=WorkState.RUNNING,
            ))
            with database.connect() as connection:
                connection.execute(
                    """UPDATE work_items SET claimed_by='old-worker',
                       claimed_at='2026-01-01T00:00:00Z' WHERE id='JOB-1'"""
                )

            status = operator_status(database)

            self.assertFalse(status["healthy"])
            self.assertEqual(
                status["next_action"], "recover_or_inspect_running_claim",
            )
            self.assertEqual(
                status["oldest_unresolved_claim"], "2026-01-01T00:00:00Z",
            )

    def test_recent_running_claim_is_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="TestStrategy",
            ))
            database.upsert_work_item(WorkItem(
                id="JOB-1", experiment_id="EXP-1", priority=1,
                state=WorkState.RUNNING,
            ))
            claimed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            with database.connect() as connection:
                connection.execute(
                    """UPDATE work_items SET claimed_by='active-worker',
                       claimed_at=? WHERE id='JOB-1'""",
                    (claimed_at,),
                )

            status = operator_status(database)

            self.assertTrue(status["healthy"])
            self.assertEqual(status["next_action"], "monitor_running_batch")
            self.assertEqual(status["running_execution_claims"], 1)
            self.assertEqual(status["unresolved_execution_claims"], 0)

    def test_stale_claim_recovery_is_preview_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="TestStrategy",
            ))
            database.upsert_work_item(WorkItem(
                id="JOB-1", experiment_id="EXP-1", priority=1,
                state=WorkState.RUNNING,
            ))
            with database.connect() as connection:
                connection.execute(
                    """UPDATE work_items SET claimed_by='old-worker',
                       claimed_at='2026-01-01T00:00:00Z' WHERE id='JOB-1'"""
                )

            preview = database.recover_stale_unexecuted_claims(
                "2026-01-02T00:00:00Z",
            )
            self.assertFalse(preview["applied"])
            self.assertEqual(preview["recoverable"][0]["id"], "JOB-1")
            self.assertEqual(
                database.rows("SELECT state FROM work_items")[0]["state"], "running",
            )

            applied = database.recover_stale_unexecuted_claims(
                "2026-01-02T00:00:00Z", apply=True,
            )
            self.assertTrue(applied["applied"])
            self.assertEqual(
                database.rows("SELECT state FROM work_items")[0]["state"], "ready",
            )


if __name__ == "__main__":
    unittest.main()
