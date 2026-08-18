from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.loop_control import SupervisorLoopControl
from ats_lab.models import ExperimentSpec, WorkItem, WorkState
from ats_lab.status import operator_status


class FakeLauncher:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def launch(self, command, *, cwd, log_path):
        self.commands.append(command)
        self.cwd = cwd
        self.log_path = log_path
        return 4321


class FailingLauncher:
    def launch(self, command, *, cwd, log_path):
        raise OSError("cannot launch")


class LoopControlTests(unittest.TestCase):
    def make_database(self, root: Path) -> WorkflowDatabase:
        database = WorkflowDatabase(root / ".ats-lab" / "laboratory.sqlite3")
        database.initialize()
        database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="StrategyOne",
        ))
        database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1",
            priority=1,
            state=WorkState.WAITING_RETRY, retry_after="30",
        ))
        return database

    def test_start_repairs_relative_retries_and_launches_detached_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self.make_database(root)
            launcher = FakeLauncher()
            control = SupervisorLoopControl(
                database, root, launcher=launcher, alive=lambda _pid: False,
                python_executable="python-test",
            )

            result = control.start()
            row = database.rows(
                "SELECT retry_after FROM work_items WHERE id='JOB-1'"
            )[0]

        self.assertEqual(result.state, "started")
        self.assertEqual(result.process_id, 4321)
        self.assertEqual(result.repaired_retry_schedules, 1)
        self.assertTrue(row["retry_after"].startswith("20"))
        self.assertIn("supervisor", launcher.commands[0])
        self.assertIn("--continuous", launcher.commands[0])

    def test_invalid_relative_retry_is_reported_as_stalled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = operator_status(self.make_database(Path(tmp)))

        self.assertFalse(status["healthy"])
        self.assertEqual(status["progress_state"], "stalled")
        self.assertEqual(status["invalid_retry_schedules"], 1)

    def test_dead_supervisor_is_not_reported_healthy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(Path(tmp))
            database.update_supervisor_runtime(
                worker_id="supervisor", process_id=99999999, phase="checking",
                started_at="2026-08-18T00:00:00Z",
            )

            status = operator_status(database)

        self.assertFalse(status["healthy"])
        self.assertEqual(status["progress_state"], "stalled")
        self.assertEqual(status["supervisor_liveness"], "stopped")
        self.assertFalse(status["supervisor_process_alive"])
        self.assertEqual(status["next_action"], "start_or_inspect_supervisor")

    def test_start_resumes_existing_process_and_stop_is_graceful(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self.make_database(root)
            database.update_supervisor_runtime(
                worker_id="supervisor", process_id=99, phase="idle",
                started_at="2026-08-02T00:00:00Z",
            )
            launcher = FakeLauncher()
            control = SupervisorLoopControl(
                database, root, launcher=launcher, alive=lambda _pid: True,
            )

            started = control.start()
            stopped = control.stop()

        self.assertEqual(started.state, "already_running")
        self.assertEqual(stopped.state, "stop_requested")
        self.assertEqual(stopped.control, "stop_requested")
        self.assertEqual(launcher.commands, [])

    def test_failed_launch_leaves_control_paused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = self.make_database(root)
            control = SupervisorLoopControl(
                database, root, launcher=FailingLauncher(),
                alive=lambda _pid: False,
            )

            with self.assertRaisesRegex(OSError, "cannot launch"):
                control.start()

            self.assertEqual(
                database.control_status()["desired_state"], "paused",
            )


if __name__ == "__main__":
    unittest.main()
