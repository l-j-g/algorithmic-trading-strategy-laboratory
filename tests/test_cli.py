import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ats_lab.cli import discover_lab_repo, emit_progress, main
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


class DiscoverLabRepoTests(unittest.TestCase):
    def test_finds_lab_root_from_nested_package_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".ats-lab").mkdir()
            (root / ".ats-lab" / "config.toml").write_text("")
            nested = root / "src" / "ats_lab"
            nested.mkdir(parents=True)

            self.assertEqual(discover_lab_repo(nested), root.resolve())

    def test_keeps_start_directory_when_no_config_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            start = Path(tmp)

            self.assertEqual(discover_lab_repo(start), start.resolve())

    def test_continuous_progress_is_compact(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            emit_progress({
                "status": "synthesized",
                "operator": {
                    "work_states": {"scheduled": 25, "ready": 8},
                    "next_action": "execute_batch",
                    "hpo": {"analyzer": {"state": "completed"}},
                    "large_unused_field": ["raw"] * 100,
                },
                "synthesis": {
                    "generated": [{"id": index} for index in range(25)],
                    "rejected": [], "submitted": 25,
                },
            })

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["synthesis"]["generated"], 25)
        self.assertEqual(payload["queue"]["ready"], 8)
        self.assertEqual(payload["analyzer_state"], "completed")
        self.assertNotIn("large_unused_field", output.getvalue())


class CliEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "lab.sqlite3"
        database = WorkflowDatabase(self.path)
        database.initialize()
        database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="CliStrategy",
        ))
        database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1,
            state=WorkState.FINISHED,
        ))
        database.add_run(RunResult(
            id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="SESSION-1", status=RunStatus.FINISHED,
            metrics={
                "sharpe_ratio": 1.5, "trade_count": 50,
                "raw_secret": "diagnostic-only",
            },
        ))
        database.add_evaluation(Evaluation(
            experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
            summary="finding", next_step="run OOS", evaluator="test",
        ))
        database.add_run(RunResult(
            id="RUN-2", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="SESSION-2", status=RunStatus.FINISHED,
            metrics={
                "sharpe_ratio": 1.1, "trade_count": 45,
                "evidence_split": "train",
            },
        ))
        self.study_id = database.schedule_hpo_candidate(
            "EXP-1", "JOB-1",
        )["id"]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> str:
        output = io.StringIO()
        argv = [
            "ats-lab", "--repo", str(self.root), "--database", str(self.path),
            *arguments,
        ]
        with patch("sys.argv", argv), redirect_stdout(output):
            self.assertEqual(main(), 0)
        return output.getvalue()

    def test_normal_evidence_and_candidates_are_human_tables(self) -> None:
        for command in (("evidence",), ("candidates",), ("status",)):
            output = self.invoke(*command)
            self.assertFalse(any(
                line.lstrip().startswith("{") for line in output.splitlines()
            ))
            self.assertNotIn("diagnostic-only", output)

    def test_candidates_deduplicate_atomic_evidence_by_experiment(self) -> None:
        human = self.invoke("candidates")
        payload = json.loads(self.invoke("candidates", "--format", "json"))
        evidence = json.loads(self.invoke("evidence", "--format", "json"))

        self.assertEqual(human.count("CliStrategy"), 1)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["experiment_id"], "EXP-1")
        self.assertEqual(len(evidence), 2)

    def test_memory_backfill_command_defaults_to_non_mutating_preview(self) -> None:
        with WorkflowDatabase(self.path).connect() as connection:
            connection.execute("DELETE FROM research_memory_outbox")

        payload = json.loads(self.invoke(
            "memory-backfill", "--dry-run", "--batch-size", "10",
        ))

        self.assertFalse(payload["apply"])
        self.assertEqual(payload["would_enqueue"], 1)
        self.assertEqual(payload["queued"], 0)
        self.assertEqual(WorkflowDatabase(self.path).rows(
            "SELECT COUNT(*) n FROM research_memory_outbox"
        )[0]["n"], 0)

    def test_normalized_json_requires_explicit_format(self) -> None:
        payload = json.loads(self.invoke("evidence", "--format", "json"))

        self.assertEqual(payload[0]["run_id"], "RUN-1")
        self.assertEqual(payload[0]["sharpe_ratio"], 1.5)
        self.assertNotIn("raw_secret", payload[0])

    def test_memory_status_and_dry_run_are_local_and_bounded(self) -> None:
        status = json.loads(self.invoke("memory-status"))
        preview = json.loads(self.invoke("memory-sync", "--dry-run", "--limit", "3"))
        self.assertEqual(status, {"delivered": 0, "pending": 1, "retry": 0})
        self.assertFalse(preview["apply"])
        self.assertEqual(preview["eligible"], 1)

    def test_raw_evidence_requires_diagnostic_export(self) -> None:
        payload = json.loads(self.invoke("diagnostic-export", "RUN-1"))

        self.assertEqual(payload["metrics"]["raw_secret"], "diagnostic-only")

    def test_hpo_lifecycle_commands_are_human_by_default(self) -> None:
        output = self.invoke("hpo")
        payload = json.loads(self.invoke("hpo", "--format", "json"))

        self.assertIn("hpo_scheduled", output)
        self.assertIn(self.study_id, output)
        self.assertFalse(any(
            line.lstrip().startswith("{") for line in output.splitlines()
        ))
        self.assertEqual(payload[0]["lifecycle_state"], "hpo_scheduled")

    def test_hpo_timings_and_analyzer_are_supervisable(self) -> None:
        with (
            patch.object(
                WorkflowDatabase, "work_item_stage_timings",
                return_value=[{
                    "work_item_id": "JOB-HPO", "stage": "hpo_analysis",
                    "attempt": 1, "state": "completed",
                    "duration_seconds": 65, "outcome": "selected",
                    "started_at": "2026-07-30T00:00:00Z",
                }],
            ),
            patch.object(
                WorkflowDatabase, "current_analyzer_status",
                return_value={
                    "job_id": "ANALYZE-1", "study_id": "HPO-1",
                    "state": "running", "attempts": 1,
                    "claimed_by": "worker", "retry_after": None,
                    "last_error": None,
                },
            ),
        ):
            timings = self.invoke("timings")
            analyzer = self.invoke("analyzer")

        self.assertIn("1m 5s", timings)
        self.assertIn("ANALYZE-1", analyzer)
        self.assertIn("running", analyzer)

    def test_hpo_trial_parameters_require_diagnostic_command(self) -> None:
        with patch.object(
            WorkflowDatabase, "diagnostic_hpo_trial_details",
            return_value={
                "study_id": "HPO-1", "trial_number": 4,
                "params": {"period": 20},
            },
        ):
            payload = json.loads(
                self.invoke("diagnostic-hpo-trial", "HPO-1", "4")
            )

        self.assertEqual(payload["params"], {"period": 20})

    def test_terminal_hpo_analysis_requeue_is_explicit(self) -> None:
        with patch.object(
            WorkflowDatabase, "requeue_terminal_hpo_analysis",
            return_value={
                "id": "ANALYZE-1", "state": "pending", "attempts": 0,
            },
        ) as requeue:
            payload = json.loads(self.invoke(
                "requeue-hpo-analysis", "ANALYZE-1",
                "--reason", "provider repaired",
                "--operator", "test",
            ))

        self.assertEqual(payload["state"], "pending")
        requeue.assert_called_once_with(
            "ANALYZE-1", reason="provider repaired", updated_by="test",
        )

    def test_validation_routes_are_configured_from_explicit_file(self) -> None:
        route_file = self.root / "validation-routes.json"
        route_file.write_text(json.dumps({
            "oos": [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "BTC-USDT", "timeframe": "1h",
                "start_date": "2026-01-01",
                "finish_date": "2026-03-31",
            }],
        }))
        with patch.object(
            WorkflowDatabase, "configure_hpo_validation_routes",
            return_value={
                "study_id": "HPO-1",
                "updated_work_items": ["VAL-1"],
                "splits": {"oos": 1},
            },
        ) as configure:
            payload = json.loads(self.invoke(
                "configure-hpo-validation-routes", "HPO-1",
                "--file", str(route_file), "--operator", "test",
            ))

        self.assertEqual(payload["updated_work_items"], ["VAL-1"])
        configure.assert_called_once()
        self.assertEqual(configure.call_args.args[0], "HPO-1")
        self.assertEqual(configure.call_args.kwargs["updated_by"], "test")


if __name__ == "__main__":
    unittest.main()
