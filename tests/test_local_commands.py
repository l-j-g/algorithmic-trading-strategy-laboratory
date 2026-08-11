import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ats_lab.local_commands import (
    ALLOWED_ACTIONS,
    LocalCommandError,
    LocalCommandRunner,
    _execute_process,
    run_local_command,
)


class LocalCommandPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        (self.repo / ".ats-lab").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_only_fixed_actions_build_explicit_python_argv(self) -> None:
        runner = LocalCommandRunner(self.repo)

        self.assertEqual(set(ALLOWED_ACTIONS), {
            "status", "preflight", "hpo_doctor", "supervisor_plan",
            "recover_claims_preview",
        })
        for action in ALLOWED_ACTIONS:
            argv = runner.argv_for(action)
            self.assertEqual(argv[:3], [runner.python_executable, "-m", "ats_lab.cli"])
            self.assertEqual(argv[3], "--repo")
            self.assertEqual(argv[5], "--database")
            self.assertNotIn("|", argv)
            self.assertNotIn(";", argv)
            self.assertNotIn("&&", argv)

    def test_unknown_action_and_input_rejected(self) -> None:
        runner = LocalCommandRunner(self.repo)

        with self.assertRaises(LocalCommandError):
            runner.run("status; env")
        with self.assertRaises(LocalCommandError):
            runner.run(3)  # type: ignore[arg-type]
        with self.assertRaises(LocalCommandError):
            run_local_command("status", self.repo, command="env")
        with self.assertRaises(LocalCommandError):
            LocalCommandRunner(self.repo, timeout_seconds="fast")  # type: ignore[arg-type]
        with self.assertRaises(LocalCommandError):
            LocalCommandRunner(self.repo, output_limit_bytes=True)  # type: ignore[arg-type]

    def test_environment_is_fixed_and_does_not_inherit_secrets(self) -> None:
        runner = LocalCommandRunner(self.repo)
        with mock.patch.dict(
            os.environ,
            {"AWS_SECRET_ACCESS_KEY": "must-not-pass", "PATH": "/tmp"},
            clear=False,
        ):
            environment = runner.sanitized_environment()

        self.assertEqual(environment["PYTHONPATH"], str(runner.repo / "src"))
        self.assertEqual(environment["PATH"], "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
        self.assertNotIn("HOME", environment)

    def test_status_action_returns_structured_result(self) -> None:
        result = LocalCommandRunner(self.repo, timeout_seconds=10).run("status")

        self.assertEqual(result["action"], "status")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["timed_out"])
        self.assertIsInstance(result["argv"], list)
        self.assertIn('"healthy"', result["output"])


class LocalCommandProcessTests(unittest.TestCase):
    def test_output_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, timed_out, output, truncated = _execute_process(
                [sys.executable, "-c", "print('x' * 10000)"],
                cwd=Path(tmp), env={"PATH": "/usr/bin:/bin"},
                timeout_seconds=5, output_limit_bytes=128,
            )

        self.assertEqual(exit_code, 0)
        self.assertFalse(timed_out)
        self.assertEqual(len(output.encode()), 128)
        self.assertTrue(truncated)

    def test_timeout_kills_process_and_returns_structured_transport_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exit_code, timed_out, output, truncated = _execute_process(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                cwd=Path(tmp), env={"PATH": "/usr/bin:/bin"},
                timeout_seconds=0.05, output_limit_bytes=128,
            )

        self.assertIsNotNone(exit_code)
        self.assertTrue(timed_out)
        self.assertIsInstance(output, str)
        self.assertFalse(truncated)


if __name__ == "__main__":
    unittest.main()
