from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    Evaluation,
    ExperimentSpec,
    ExperimentType,
    Verdict,
    WorkItem,
    WorkState,
)


class HpoPersistenceTests(unittest.TestCase):
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
            state=WorkState.FINISHED,
        ))
        self.database.add_evaluation(Evaluation(
            experiment_id="EXP-1",
            verdict=Verdict.HPO_CANDIDATE,
            evaluator="test",
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidate_schedule_completion_retry_and_terminal_disposition(self) -> None:
        candidates = self.database.hpo_studies(
            {"lifecycle_state": "hpo_candidate"},
        )
        self.assertEqual(candidates[0]["parent_experiment_id"], "EXP-1")

        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        repeated = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.assertEqual(study["id"], repeated["id"])
        self.assertEqual(study["lifecycle_state"], "hpo_scheduled")
        self.assertEqual(
            self.database.hpo_study_for_work_item(
                study["hpo_work_item_id"],
            )["study_id"],
            study["id"],
        )

        self.database.start_hpo_study(study["id"])
        job = self.database.complete_hpo_study(study["id"])
        claimed = self.database.claim_hpo_analysis(
            "analyzer", cohort_id="COHORT-1",
        )
        self.assertEqual(job["id"], claimed["id"])
        self.assertEqual(claimed["attempts"], 1)
        retry = self.database.retry_hpo_analysis(
            claimed["id"],
            error="temporary",
            retry_after="2000-01-01T00:00:00Z",
        )
        self.assertEqual(retry["state"], "waiting_retry")
        claimed = self.database.claim_hpo_analysis("analyzer")
        final = self.database.terminalize_hpo_analysis(
            claimed["id"],
            disposition="revise",
            finding="Needs wider validation.",
            next_action="Revise ranges.",
        )
        self.assertEqual(final["lifecycle_state"], "revise")
        self.assertEqual(final["disposition"], "revise")

    def test_abandoned_analysis_is_recoverable(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.complete_hpo_study(study["id"])
        job = self.database.claim_hpo_analysis("analyzer")
        self.database.abandon_hpo_analysis(job["id"], error="worker lost")

        recovered = self.database.recover_abandoned_hpo_analysis(
            "9999-12-31T00:00:00Z",
        )

        self.assertEqual([row["id"] for row in recovered], [job["id"]])
        self.assertEqual(
            self.database.current_analyzer_status()["state"], "pending",
        )

    def test_unroutable_running_study_returns_to_scheduled(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.start_hpo_study(study["id"])
        work_item_id = study["hpo_work_item_id"]
        self.database.promote_scheduled_runnable(1)

        changed = self.database.mark_unroutable_hpo_requirements_pending()

        self.assertEqual(changed, 1)
        refreshed = self.database.hpo_study_detail(study["id"])
        self.assertEqual(refreshed["lifecycle_state"], "hpo_scheduled")
        self.assertIsNone(refreshed["started_at"])
        work = self.database.rows(
            "SELECT state,blocker_code FROM work_items WHERE id=?",
            (work_item_id,),
        )[0]
        self.assertEqual(work["state"], "ready")
        self.assertEqual(work["blocker_code"], "requirements_pending")

    def test_terminal_analysis_can_be_explicitly_requeued(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.complete_hpo_study(study["id"])
        job = self.database.claim_hpo_analysis("analyzer")
        terminal = self.database.retry_hpo_analysis(
            job["id"], error="provider quota",
            retry_after="2000-01-01T00:00:00Z", max_attempts=1,
        )

        reopened = self.database.requeue_terminal_hpo_analysis(
            terminal["id"], reason="fallback provider repaired",
            updated_by="test",
        )

        self.assertEqual(reopened["state"], "pending")
        self.assertEqual(reopened["attempts"], 0)
        self.assertIsNone(reopened["last_error"])
        self.assertEqual(
            self.database.hpo_studies(
                {"lifecycle_state": "hpo_analysis"},
            )[0]["study_id"],
            study["id"],
        )
        event = self.database.rows(
            """SELECT payload_json FROM events
               WHERE aggregate_id=? AND event_type='hpo_analysis_requeued'""",
            (job["id"],),
        )[0]
        self.assertIn("fallback provider repaired", event["payload_json"])

    def test_stage_timing_returns_normalized_shape(self) -> None:
        timing = self.database.record_work_item_stage(
            "JOB-1",
            "hpo_analysis",
            "2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:02Z",
            analyzer_attempt=2,
            cohort_id="COHORT-1",
            outcome="complete",
            detail={"trials": 50},
        )
        rows = self.database.recent_stage_timings()

        self.assertEqual(rows[0]["id"], timing["id"])
        self.assertEqual(rows[0]["attempt"], 2)
        self.assertEqual(rows[0]["duration_seconds"], 2.0)
        self.assertEqual(rows[0]["completed_at"], "2026-01-01T00:00:02Z")
        self.assertEqual(rows[0]["detail"], {"trials": 50})
        self.assertNotIn("detail_json", rows[0])
        self.assertNotIn("duration_ms", rows[0])


if __name__ == "__main__":
    unittest.main()
