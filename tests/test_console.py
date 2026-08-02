from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from ats_lab.console import (
    monitor_snapshot,
    render_analyzer,
    render_hpo_detail,
    render_hpo_studies,
    render_monitor,
    render_stage_timings,
    run_console,
)
from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    Evaluation,
    ExperimentSpec,
    RunResult,
    RunStatus,
    Verdict,
    WorkItem,
    WorkState,
)


class TerminalConsoleTests(unittest.TestCase):
    def make_database(self, root: str) -> WorkflowDatabase:
        database = WorkflowDatabase(Path(root) / "workflow.sqlite3")
        database.initialize()
        database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="TestStrategy",
        ))
        database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1,
            state=WorkState.READY,
        ))
        return database

    def test_monitor_renders_control_queue_and_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)

            rendered = render_monitor(monitor_snapshot(database))

            self.assertIn("CONTROL running", rendered)
            self.assertIn("ready=1", rendered)
            self.assertIn("JOB-1", rendered)
            self.assertIn("control pause", rendered)

    def test_console_pause_status_resume_and_quit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            input_stream = io.StringIO("pause\nstatus\nresume\nquit\n")
            output = io.StringIO()

            code = run_console(
                database, input_stream=input_stream, output=output,
            )

            self.assertEqual(code, 0)
            self.assertEqual(database.control_status()["desired_state"], "running")
            self.assertIn("CONTROL paused", output.getvalue())
            self.assertIn("CONTROL running", output.getvalue())
            self.assertNotIn('{"desired_state"', output.getvalue())

    def test_console_candidates_are_human_normalized_not_raw_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            database.add_run(RunResult(
                id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
                session_id="SESSION-1", status=RunStatus.FINISHED,
                metrics={
                    "sharpe_ratio": 1.2, "trade_count": 30,
                    "raw_secret": "diagnostic-only",
                },
            ))
            database.add_evaluation(Evaluation(
                experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
                summary="promising", next_step="run OOS", evaluator="test",
            ))
            output = io.StringIO()

            code = run_console(
                database, input_stream=io.StringIO("candidates\nquit\n"),
                output=output,
            )

            self.assertEqual(code, 0)
            self.assertIn("Sharpe", output.getvalue())
            self.assertIn("TestStrategy", output.getvalue())
            self.assertNotIn("raw_secret", output.getvalue())
            self.assertNotIn("diagnostic-only", output.getvalue())
            self.assertFalse(any(
                line.lstrip().startswith("{")
                for line in output.getvalue().splitlines()
            ))

    def test_hpo_lifecycle_analyzer_and_timings_are_human(self) -> None:
        lifecycle = render_hpo_studies([{
            "study_id": "HPO-1", "strategy": "TestStrategy",
            "lifecycle_state": "hpo_analysis", "objective_name": "sharpe",
            "completed_trial_count": 80, "trial_count": 100,
            "selected_trial_count": 4, "validation_count": 1,
            "disposition": None, "next_action": "validate",
        }])
        analyzer = render_analyzer({
            "job_id": "ANALYZE-1", "study_id": "HPO-1",
            "state": "running", "attempts": 2, "claimed_by": "worker",
            "retry_after": None, "last_error": None,
        })
        timings = render_stage_timings([{
            "work_item_id": "JOB-HPO", "stage": "hpo_analysis",
            "attempt": 2, "state": "completed", "duration_seconds": 90,
            "outcome": "selected", "started_at": "2026-07-30T00:00:00Z",
        }])

        self.assertIn("hpo_analysis", lifecycle)
        self.assertIn("80", lifecycle)
        self.assertIn("ANALYZE-1", analyzer)
        self.assertIn("1m 30s", timings)
        self.assertNotIn("{", lifecycle + analyzer + timings)

    def test_hpo_detail_shows_validation_readiness_without_raw_json(self) -> None:
        output = render_hpo_detail({
            "study_id": "HPO-1", "strategy": "TestStrategy",
            "lifecycle_state": "validation",
            "selected_trials": [],
            "validations": [{
                "state": "scheduled", "experiment_id": "VAL-1",
                "readiness_status": "requirements_pending",
                "blocker_detail": "validation routes required",
            }],
            "timings": [],
            "analysis_job": None,
        })

        self.assertIn("requirements_pending", output)
        self.assertIn("validation routes required", output)
        self.assertNotIn("{", output)


if __name__ == "__main__":
    unittest.main()
