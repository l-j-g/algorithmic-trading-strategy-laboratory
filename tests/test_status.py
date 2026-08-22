from __future__ import annotations

import json
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

    def test_route_readiness_groups_validation_jobs_across_studies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="TestStrategy",
            ))
            database.upsert_work_item(WorkItem(
                id="HPO-A-JOB", experiment_id="EXP-1", priority=1,
                state=WorkState.RUNNING,
            ))
            database.upsert_work_item(WorkItem(
                id="HPO-B-JOB", experiment_id="EXP-1", priority=1,
                state=WorkState.RUNNING,
            ))
            database.upsert_work_item(WorkItem(
                id="VAL-A1", experiment_id="EXP-1", priority=2,
                state=WorkState.READY,
            ))
            database.upsert_work_item(WorkItem(
                id="VAL-A2", experiment_id="EXP-1", priority=3,
                state=WorkState.SCHEDULED,
            ))
            database.upsert_work_item(WorkItem(
                id="VAL-B1", experiment_id="EXP-1", priority=4,
                state=WorkState.FINISHED,
            ))
            with database.connect() as connection:
                for study_id in ("HPO-A", "HPO-B"):
                    connection.execute(
                        """INSERT INTO hpo_studies(
                               id,study_name,strategy,hpo_experiment_id,
                               hpo_work_item_id,lifecycle_state,created_at,updated_at
                           ) VALUES (?,?,?,?,?,'validation','2026-08-01T00:00:00Z',
                                     '2026-08-01T00:00:00Z')""",
                        (
                            study_id, f"study-{study_id}", "TestStrategy",
                            "EXP-1", f"{study_id}-JOB",
                        ),
                    )
                jobs = (
                    ("VJ-A1", "HPO-A", "VAL-A1", "ready", None),
                    ("VJ-A2", "HPO-A", "VAL-A2", "scheduled", "requirements_pending"),
                    ("VJ-B1", "HPO-B", "VAL-B1", "finished", None),
                )
                for index, (job_id, study_id, work_item_id, state, readiness) in enumerate(jobs):
                    connection.execute(
                        """INSERT INTO hpo_trials(
                               id,study_id,trial_number,state,params_json,imported_at
                           ) VALUES (?,?,?,'COMPLETE','{}','2026-08-01T00:00:00Z')""",
                        (f"trial-{job_id}", study_id, -(index + 1)),
                    )
                    connection.execute(
                        """INSERT INTO hpo_validation_jobs(
                               id,study_id,trial_id,work_item_id,evidence_split,
                               state,created_at
                           ) VALUES (?,?,?,?,'oos',?,'2026-08-01T00:00:00Z')""",
                        (job_id, study_id, f"trial-{job_id}", work_item_id, state),
                    )
                    connection.execute(
                        """UPDATE work_items SET specification_json=?
                           WHERE id=?""",
                        (json.dumps({"readiness": {"status": readiness}}), work_item_id),
                    )

            readiness = operator_status(database)["hpo"]["route_readiness"]

            per_study = {
                entry["study_id"]: entry["validation_jobs"]
                for entry in readiness["studies"]
            }
            self.assertEqual(per_study, {"HPO-A": 2, "HPO-B": 1})
            self.assertEqual(readiness["validation_jobs"]["total"], 3)
            self.assertEqual(readiness["validation_jobs"]["ready"], 2)
            self.assertEqual(readiness["validation_jobs"]["pending"], 1)
            self.assertEqual(readiness["validation_jobs"]["finished"], 1)


if __name__ == "__main__":
    unittest.main()
