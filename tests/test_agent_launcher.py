from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ats_lab.agent_launcher import AgentLauncherConfig, build_prompt, launch, load_config


class AgentLauncherTests(unittest.TestCase):
    def test_loads_ignored_local_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "jesse"
            repo.mkdir()
            config = root / "config.toml"
            config.write_text(
                f'[repositories]\njesse = "{repo}"\n\n[executor]\nprofile = "ats-lab"\ntimeout_seconds = 12\n'
                'toolsets = ["mcp"]\n'
            )
            loaded = load_config(config)
            self.assertEqual(loaded.repository, repo.resolve())
            self.assertEqual(loaded.timeout_seconds, 12)
            self.assertEqual(loaded.profile, "ats-lab")
            self.assertEqual(loaded.toolsets, ("mcp",))

    def test_prompt_states_ownership_boundaries(self) -> None:
        prompt = build_prompt({"work_item_id": "JOB-1"})
        self.assertIn("Memory", prompt)
        self.assertIn("memory/context only", prompt)
        self.assertIn("Jesse MCP only", prompt)
        self.assertIn('"work_item_id":"JOB-1"', prompt)

    def test_synthesis_prompt_requires_typed_job_request(self) -> None:
        prompt = build_prompt({"task_type": "synthesize_batch", "context": {}})
        self.assertIn('"synthesis_requests"', prompt)
        self.assertIn("New or changed entry rules", prompt)
        self.assertIn("promotion-locked", prompt)
        self.assertIn("normally 25", prompt)

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_launch_uses_argv_without_shell_and_configured_cwd(self, _which) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps({"outcome": "finished", "evidence": {}}), "")

        config = AgentLauncherConfig(Path("/tmp/jesse"), profile="ats-lab", toolsets=("mcp",))
        result = launch({"work_item_id": "JOB-1"}, config, runner=runner, environment={"SAFE": "1"})
        self.assertEqual(result["outcome"], "finished")
        command, kwargs = calls[0]
        self.assertEqual(command[0], "/bin/executor")
        self.assertEqual(command[1:3], ["-p", "ats-lab"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["cwd"], Path("/tmp/jesse"))
        self.assertEqual(kwargs["env"], {"SAFE": "1"})

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_timeout_becomes_retry(self, _which) -> None:
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        result = launch({}, AgentLauncherConfig(Path("/tmp")), runner=runner)
        self.assertEqual(result["outcome"], "retry")
        self.assertEqual(result["blocker_code"], "executor_timeout")

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_non_json_output_becomes_retry(self, _which) -> None:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "not json", "")

        result = launch({}, AgentLauncherConfig(Path("/tmp")), runner=runner)
        self.assertEqual(result["blocker_code"], "invalid_executor_result")


if __name__ == "__main__":
    unittest.main()
