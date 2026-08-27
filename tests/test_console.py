from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from ats_lab.console import (
    monitor_snapshot,
    render_completion_table,
    render_analyzer,
    render_hpo_detail,
    render_hpo_studies,
    render_monitor,
    render_stage_timings,
    run_console,
)
from ats_lab.database import WorkflowDatabase
from ats_lab.humanize import human_time
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

    def test_monitor_shows_recent_completed_job_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            database.add_run(RunResult(
                id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
                session_id="SESSION-1", status=RunStatus.FINISHED,
                route={
                    "exchange": "Binance Perpetual Futures",
                    "symbol": "BTC-USDT", "timeframe": "1h",
                    "start_date": "2026-01-01", "finish_date": "2026-02-01",
                },
                metrics={
                    "net_profit_percentage": 12.345,
                    "trade_count": 42,
                    "sharpe_ratio": 1.23,
                    "max_drawdown_percentage": -4.5,
                },
                finished_at="2026-02-01T00:00:00Z",
            ))

            rendered = render_monitor(monitor_snapshot(database))

            self.assertIn("COMPLETED", rendered)
            self.assertIn("TestStrategy", rendered)
            self.assertIn("BTC-USDT 1h", rendered)
            self.assertIn("+12.35%", rendered)
            self.assertIn("42", rendered)
            self.assertIn("1.23", rendered)
            self.assertIn("4.50%", rendered)

    def test_monitor_prioritizes_metric_results_over_empty_terminal_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            for index in range(20):
                database.add_run(RunResult(
                    id=f"EMPTY-{index}", experiment_id="EXP-1", work_item_id="JOB-1",
                    session_id=f"EMPTY-SESSION-{index}", status=RunStatus.FINISHED,
                    finished_at=f"2026-03-{index + 1:02d}T00:00:00Z",
                ))
            database.add_run(RunResult(
                id="METRIC-OLD", experiment_id="EXP-1", work_item_id="JOB-1",
                session_id="METRIC-SESSION", status=RunStatus.FINISHED,
                metrics={"net_profit_percentage": 9.5, "trade_count": 17},
                finished_at="2026-01-01T00:00:00Z",
            ))

            snapshot = monitor_snapshot(database)

            self.assertEqual(len(snapshot["recent_completions"]), 8)
            self.assertIn(
                "METRIC-OLD",
                {row["run_id"] for row in snapshot["recent_completions"]},
            )
            self.assertTrue(any(
                row.get("net_profit_percentage") == 9.5
                for row in snapshot["recent_completions"]
            ))

    def test_completion_table_fits_narrow_terminal_without_misalignment(self) -> None:
        rendered = render_completion_table(({
            "strategy": "VeryLongStrategyNameThatNeedsTruncation",
            "symbol": "BTC-USDT", "timeframe": "1h",
            "net_profit_percentage": 3.1, "trade_count": 10,
            "sharpe_ratio": 0.9, "max_drawdown_percentage": -2.2,
            "verdict": "revise",
        },), width=64)

        lines = rendered.splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(len(line) <= 64 for line in lines))
        self.assertIn("strategy", lines[0])
        self.assertIn("pair", lines[0])
        self.assertIn("…", rendered)

    def test_live_monitor_is_compact_and_hides_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            rendered = render_monitor(
                monitor_snapshot(database), width=72, compact=True,
            )

            self.assertIn("ATS LAB", rendered)
            self.assertNotIn("ATS LAB LIVE", rendered)
            self.assertIn("STATUS", rendered)
            self.assertIn("QUEUE", rendered)
            self.assertIn("ACTIVE", rendered)
            self.assertIn("└─", rendered)
            self.assertIn("TestStrategy", rendered)
            self.assertNotIn("CANDIDATES", rendered)
            self.assertNotIn("NEXT   ", rendered)
            self.assertNotIn("ANALYZER", rendered)
            self.assertNotIn("{", rendered)
            self.assertTrue(all(len(line) <= 72 for line in rendered.splitlines()))

    def test_live_monitor_color_is_opt_in_at_renderer_edge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            plain = render_monitor(monitor_snapshot(database), compact=True)
            colored = render_monitor(
                monitor_snapshot(database), compact=True, color=True,
            )

            self.assertNotIn("\033[", plain)
            self.assertIn("\033[", colored)
            self.assertIn("ATS LAB", colored)
            self.assertNotIn("ATS LAB LIVE", colored)

    def test_console_watch_rejects_non_numeric_interval_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            output = io.StringIO()

            code = run_console(
                database,
                input_stream=io.StringIO("watch abc\nquit\n"),
                output=output,
            )

            self.assertEqual(code, 0)
            self.assertIn("invalid interval: abc", output.getvalue())

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


class HumanTimeTests(unittest.TestCase):
    def test_timestamps_render_local_minute_precision_without_seconds(self) -> None:
        from datetime import datetime, timezone

        rendered = human_time("2026-08-11T01:20:37Z")

        self.assertNotIn("T", rendered)
        self.assertNotIn("Z", rendered)
        self.assertEqual(rendered.count(":"), 1)
        parsed = datetime.strptime(rendered, "%Y-%m-%d %H:%M").replace(
            tzinfo=datetime.now().astimezone().tzinfo,
        )
        self.assertEqual(
            parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H"),
            "2026-08-11 01",
        )

    def test_naive_values_are_treated_as_utc_and_invalid_input_passes_through(self) -> None:
        from datetime import datetime, timezone

        naive = human_time("2026-08-11T01:20:00")
        explicit = human_time("2026-08-11T01:20:00+00:00")
        self.assertEqual(naive, explicit)
        parsed = datetime.strptime(naive, "%Y-%m-%d %H:%M").replace(
            tzinfo=timezone.utc,
        )
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(parsed))

        self.assertEqual(human_time("not-a-timestamp"), "not-a-timestamp")
        self.assertEqual(human_time(None), "—")
        self.assertEqual(human_time(""), "—")


if __name__ == "__main__":
    unittest.main()
