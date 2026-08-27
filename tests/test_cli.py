import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ats_lab.cli import COMMAND_HANDLERS, build_parser, discover_lab_repo, emit_progress, main
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

    def test_uses_installed_checkout_fallback_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            start = root / "elsewhere"
            fallback = root / "installed-repo"
            start.mkdir()
            (fallback / ".ats-lab").mkdir(parents=True)
            (fallback / ".ats-lab" / "config.toml").write_text("")

            self.assertEqual(
                discover_lab_repo(start, fallback=fallback), fallback.resolve(),
            )

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

    def test_bare_cli_shows_operator_home_and_exact_next_command(self) -> None:
        output = self.invoke()

        self.assertIn("ATS LAB", output)
        self.assertIn("QUEUE", output)
        self.assertIn("MEMORY", output)
        self.assertIn("NEXT", output)
        self.assertIn("ats-lab memory sync", output)
        self.assertNotIn('"work_states"', output)

    def test_next_command_supports_human_and_json_guidance(self) -> None:
        human = self.invoke("next")
        payload = json.loads(self.invoke("next", "--format", "json"))

        self.assertIn("WHY", human)
        self.assertIn("RUN", human)
        self.assertEqual(payload["command"], "ats-lab memory sync")
        self.assertEqual(payload["reason"], "research memory is waiting for delivery")

    def test_root_help_is_curated_for_daily_operation(self) -> None:
        output = io.StringIO()
        with (
            patch("sys.argv", ["ats-lab", "--help"]),
            redirect_stdout(output),
            self.assertRaises(SystemExit) as stopped,
        ):
            main()

        self.assertEqual(stopped.exception.code, 0)
        text = output.getvalue()
        self.assertIn("START HERE", text)
        self.assertIn("ats-lab doctor", text)
        self.assertIn("ats-lab monitor --watch", text)
        self.assertIn("Use ats-lab <command> --help", text)

    def test_loop_status_is_human_readable(self) -> None:
        status = type("Status", (), {
            "to_dict": lambda self: {
                "state": "running", "process_id": 123,
                "phase": "idle", "control": "running",
                "repaired_retry_schedules": 0,
            },
        })()
        with patch("ats_lab.cli.SupervisorLoopControl") as lifecycle:
            lifecycle.return_value.status.return_value = status
            output = self.invoke("loop", "status")

        self.assertIn("LOOP RUNNING", output)
        self.assertIn("pid=123", output)

    def test_start_attaches_activity_follower_after_starting_loop(self) -> None:
        with (
            patch("ats_lab.cli.SupervisorLoopControl") as lifecycle,
            patch("ats_lab.cli.ActivityFollower") as follower,
        ):
            lifecycle.return_value.start.return_value = SimpleNamespace(
                state="started", process_id=123,
            )
            output = self.invoke("start", "--interval", "0.1")

        lifecycle.return_value.start.assert_called_once()
        follower.return_value.run.assert_called_once_with()
        self.assertEqual(output, "")

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

    def test_nested_memory_init_has_safe_dry_run(self) -> None:
        with WorkflowDatabase(self.path).connect() as connection:
            connection.execute("DELETE FROM research_memory_outbox")

        payload = json.loads(self.invoke(
            "memory", "init", "--dry-run", "--format", "json",
        ))

        self.assertFalse(payload["apply"])
        self.assertEqual(payload["would_queue"], 1)
        self.assertEqual(payload["would_deliver"], 1)
        self.assertEqual(WorkflowDatabase(self.path).rows(
            "SELECT COUNT(*) n FROM research_memory_outbox"
        )[0]["n"], 0)

    def test_nested_memory_status_is_human_by_default(self) -> None:
        human = self.invoke("memory", "status")
        payload = json.loads(self.invoke(
            "memory", "status", "--format", "json",
        ))

        self.assertIn("MEMORY", human)
        self.assertIn("pending=1", human)
        self.assertIn("ats-lab memory sync", human)
        self.assertEqual(payload, {"delivered": 0, "pending": 1, "retry": 0})

    def test_doctor_combines_checks_and_next_action(self) -> None:
        class HealthyPreflight:
            def check(self):
                return {
                    "healthy": True,
                    "checks": [
                        {"name": "docker_daemon", "status": "healthy"},
                        {"name": "memory_api", "status": "healthy"},
                    ],
                }

        with patch("ats_lab.cli.build_stack_preflight", return_value=HealthyPreflight()):
            output = self.invoke("doctor")

        self.assertIn("ATS LAB DOCTOR", output)
        self.assertIn("[OK] docker_daemon", output)
        self.assertIn("[OK] canonical_workflow", output)
        self.assertIn("ats-lab memory sync", output)

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

    def test_complete_jesse_session_export_import_command(self) -> None:
        database = WorkflowDatabase(self.path)
        study = database.hpo_studies({"id": self.study_id})[0]
        source = self.root / "jesse-session.json"
        source.write_text(json.dumps({
            "schema_version": 1,
            "source": "jesse_optimization_session",
            "session_id": "session-cli-test",
            "study_name": study["name"],
            "direction": "maximize",
            "status": "completed",
            "trial_records_complete": True,
            "total_trials": 1,
            "completed_trials": 1,
            "trials": [{
                "number": 0,
                "state": "COMPLETE",
                "objective_value": 0.5,
                "params": {"period": 12},
                "training_metrics": {"sharpe_ratio": 1.2},
                "testing_metrics": {"sharpe_ratio": 0.8},
            }],
        }))

        payload = json.loads(self.invoke(
            "hpo-import-jesse-session", self.study_id,
            "--file", str(source), "--format", "json",
        ))

        self.assertEqual(payload["study_id"], self.study_id)
        self.assertEqual(payload["source_session_id"], "session-cli-test")
        self.assertEqual(payload["trials_imported"], 1)

    def test_hpo_doctor_shows_missing_routes_and_next_command(self) -> None:
        human = self.invoke("hpo", "--doctor")
        payload = json.loads(self.invoke("hpo", "--doctor", "--format", "json"))

        self.assertIn("HPO ROUTES", human)
        self.assertIn("missing", human)
        self.assertIn("configure-hpo-validation-routes", human)
        self.assertEqual(payload["next_action"], "configure_hpo_validation_routes")
        self.assertEqual(payload["missing_routes"]["hpo"], 1)

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

    def test_hpo_route_plan_is_read_only_and_operator_friendly(self) -> None:
        human = self.invoke("hpo-route-plan", self.study_id)
        payload = json.loads(self.invoke(
            "hpo-route-plan", self.study_id, "--format", "json",
        ))

        self.assertIn("HPO ROUTES", human)
        self.assertIn("hpo", human)
        self.assertIn("configure-hpo-validation-routes", human)
        self.assertEqual(payload["study_id"], self.study_id)
        self.assertEqual(
            set(payload["required_file_shape"]), {"hpo", "oos", "rolling"},
        )

    def test_hpo_defaults_are_visible_and_explicitly_applicable(self) -> None:
        config_dir = self.root / ".ats-lab"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.toml").write_text(
            '[resources.evaluation_windows]\nas_of_date = "2026-04-01"\n',
        )
        preview = self.invoke("hpo-defaults")
        self.assertIn("->", preview)
        self.assertIn("hpo", preview)
        self.assertIn("NEXT", preview)
        payload = json.loads(self.invoke(
            "hpo-defaults", "--apply", "--format", "json",
        ))
        self.assertEqual(payload["applied"], [self.study_id])
        self.assertEqual(
            payload["policy"]["rolling"][0]["finish_date"],
            (date.fromisoformat(payload["policy"]["oos"][0]["start_date"])
             - timedelta(days=1)).isoformat(),
        )
        self.assertEqual(
            payload["policy"]["oos"][0]["finish_date"], "2026-04-01",
        )


class CliDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "lab.sqlite3"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke_raw(self, *arguments: str) -> tuple[int, str, str]:
        output = io.StringIO()
        errors = io.StringIO()
        argv = [
            "ats-lab", "--repo", str(self.root), "--database", str(self.path),
            *arguments,
        ]
        with (
            patch("sys.argv", argv),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = main()
        return code if code is not None else 0, output.getvalue(), errors.getvalue()

    def test_every_registered_command_has_exactly_one_handler(self) -> None:
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )

        self.assertEqual(
            set(subparsers.choices) - {"help"},
            set(COMMAND_HANDLERS) - {"home"},
        )

    def test_enqueue_persists_matching_contract(self) -> None:
        contract = self.root / "contract.json"
        contract.write_text(json.dumps({
            "schema_version": 1,
            "experiment": {
                "id": "EXP-A", "strategy_name": "Dispatch",
                "experiment_type": "baseline",
            },
            "work_item": {"id": "JOB-A", "experiment_id": "EXP-A"},
        }))

        code, output, errors = self.invoke_raw("enqueue", "--file", str(contract.resolve()))

        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(json.loads(output), {
            "experiment_id": "EXP-A", "work_item_id": "JOB-A",
            "state": "scheduled",
        })

    def test_enqueue_contract_violation_reports_clean_error(self) -> None:
        contract = self.root / "contract.json"
        contract.write_text(json.dumps({
            "schema_version": 1,
            "experiment": {
                "id": "EXP-A", "strategy_name": "Dispatch",
                "experiment_type": "baseline",
            },
            "work_item": {"id": "JOB-A", "experiment_id": "EXP-B"},
        }))

        code, output, errors = self.invoke_raw("enqueue", "--file", str(contract.resolve()))

        self.assertEqual(code, 2)
        self.assertIn(
            "work_item.experiment_id must equal experiment.id", errors,
        )
        self.assertNotIn("Traceback", errors)
        self.assertNotIn("Traceback", output)

    def test_synthesize_missing_file_reports_clean_error(self) -> None:
        code, _, errors = self.invoke_raw(
            "synthesize", "--file", str(self.root / "absent.json"),
        )

        self.assertEqual(code, 2)
        self.assertIn("No such file or directory", errors)
        self.assertNotIn("Traceback", errors)

    def test_synthesize_invalid_payload_reports_clean_error(self) -> None:
        proposal = self.root / "proposal.json"
        proposal.write_text("{ not json")

        code, _, errors = self.invoke_raw("synthesize", "--file", str(proposal))

        self.assertEqual(code, 2)
        self.assertNotIn("Traceback", errors)

    def test_recover_orphaned_replacements_preview_and_apply(self) -> None:
        database = WorkflowDatabase(self.path)
        database.initialize()
        database.upsert_experiment(ExperimentSpec(
            id="EXP-R", strategy_name="RecoveryStrategy",
        ))
        database.upsert_work_item(WorkItem(
            id="JOB-R", experiment_id="EXP-R", priority=1,
            state=WorkState.WAITING_RETRY,
        ))
        stamp = "2026-08-22T00:00:00Z"
        with database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_recoveries(
                       work_item_id,old_session_id,old_state,reason,
                       replacement_allowed,replacement_reserved,
                       replacement_session_id,created_at,updated_at)
                   VALUES('JOB-R','OLD-1','running','transport_failure',
                          1,1,NULL,?,?)""",
                (stamp, stamp),
            )

        preview_code, preview_output, _ = self.invoke_raw(
            "recover-orphaned-replacements",
        )
        preview = json.loads(preview_output)

        self.assertEqual(preview_code, 0)
        self.assertEqual(
            [item["work_item_id"] for item in preview["planned"]], ["JOB-R"],
        )
        self.assertEqual(preview["changed"], [])

        apply_code, apply_output, _ = self.invoke_raw(
            "recover-orphaned-replacements", "--apply",
        )
        applied = json.loads(apply_output)
        cleared = database.rows(
            """SELECT replacement_reserved FROM direct_execution_recoveries
               WHERE work_item_id='JOB-R'""",
        )

        self.assertEqual(apply_code, 0)
        self.assertEqual(applied["changed"], ["JOB-R"])
        self.assertEqual(cleared[0]["replacement_reserved"], 0)


if __name__ == "__main__":
    unittest.main()
