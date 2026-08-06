from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ats_lab.agent_launcher import (
    AgentLauncherConfig,
    build_command,
    build_prompt,
    launch,
    load_config,
)


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
            self.assertEqual(loaded.execution_toolsets, ("jesse",))
            self.assertEqual(loaded.analysis_toolsets, ("context_engine",))
            self.assertEqual(loaded.synthesis_toolsets, ("context_engine",))

    def test_loads_task_specific_toolset_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "jesse"
            repo.mkdir()
            config = root / "config.toml"
            config.write_text(
                f'[repositories]\njesse = "{repo}"\n\n[executor]\n'
                'execution_toolsets = ["jesse", "file"]\n'
                'analysis_toolsets = ["context_engine"]\n'
                'synthesis_toolsets = ["context_engine"]\n'
            )

            loaded = load_config(config)

            self.assertEqual(loaded.execution_toolsets, ("jesse", "file"))
            self.assertEqual(loaded.analysis_toolsets, ("context_engine",))
            self.assertEqual(loaded.synthesis_toolsets, ("context_engine",))

    def test_prompt_states_ownership_boundaries(self) -> None:
        prompt = build_prompt({"work_item_id": "JOB-1"})
        self.assertIn("Memory", prompt)
        self.assertIn("memory/context only", prompt)
        self.assertIn("Jesse MCP only", prompt)
        self.assertIn('"work_item_id":"JOB-1"', prompt)

    def test_execution_prompt_requires_exact_raw_session_and_safe_notional(self) -> None:
        prompt = build_prompt({"task_type": "execute_batch", "work_items": []})

        self.assertIn("raw_result", prompt)
        self.assertIn("exact data.session.metrics object", prompt)
        self.assertIn("Do not include trades", prompt)
        self.assertIn("95% of available margin", prompt)
        self.assertIn("not a Jesse configuration failure", prompt)

    def test_synthesis_prompt_requires_typed_job_request(self) -> None:
        prompt = build_prompt({"task_type": "synthesize_batch", "context": {}})
        self.assertIn('"synthesis_requests"', prompt)
        self.assertIn("New or changed entry rules", prompt)
        self.assertIn("promotion-locked", prompt)
        self.assertIn("normally 25", prompt)

    def test_batch_execution_and_analysis_have_separate_contracts(self) -> None:
        execution = build_prompt({"task_type": "execute_batch", "requests": []})
        analysis = build_prompt({
            "task_type": "analyze_batch",
            "executions": [{
                "experiment_id": "EXP-1",
                "evidence": [{
                    "strategy": "TestStrategy",
                    "net_profit_percentage": 5.0,
                    "trade_count": 12,
                }],
            }],
        })

        self.assertIn('"results"', execution)
        self.assertNotIn('"evaluations"', execution)
        self.assertIn("execution_context.optimizer_parameters", execution)
        self.assertIn("never overwrite strategy defaults", execution)
        self.assertIn('"evaluations"', analysis)
        self.assertIn('"synthesis_requests"', analysis)
        self.assertIn('"finding"', analysis)
        self.assertIn('"next_action"', analysis)
        self.assertNotIn("metrics_summary", analysis)
        self.assertIn('"net_profit_percentage":5.0', analysis)
        self.assertNotIn("route_runs", analysis)

        hpo = build_prompt({
            "task_type": "analyze_hpo", "executions": [],
        })
        self.assertIn("stable", hpo)
        self.assertIn("do not select a single lucky trial", hpo)
        self.assertIn("separate task types", hpo)

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_provider_failure_stdout_is_precise_infrastructure_blocker(
        self, _which,
    ) -> None:
        def runner(command, **kwargs):
            usage = Path(command[command.index("--usage-file") + 1])
            usage.write_text(json.dumps({
                "failed": True, "api_calls": 1,
                "provider": "openrouter", "model": "openrouter/auto",
            }))
            return subprocess.CompletedProcess(
                command, 0,
                "Provider request failed: PermissionDeniedError (status 403)\n",
                "",
            )

        result = launch(
            {"task_type": "analyze_batch"},
            AgentLauncherConfig(Path("/tmp")), runner=runner,
        )

        self.assertEqual(result["outcome"], "retry")
        self.assertEqual(result["blocker_code"], "executor_provider_failed")
        self.assertIn("provider=openrouter", result["detail"])
        self.assertIn("model=openrouter/auto", result["detail"])
        self.assertNotIn("Provider request failed", result["detail"])

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_finished_analysis_requires_evaluations_contract(self, _which) -> None:
        runner = Mock(return_value=subprocess.CompletedProcess(
            [], 0, '{"outcome":"finished"}', "",
        ))
        result = launch(
            {"task_type": "analyze_batch"},
            AgentLauncherConfig(Path("/tmp")), runner=runner,
        )
        self.assertEqual(result["blocker_code"], "invalid_executor_contract")

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_launch_uses_argv_without_shell_and_configured_cwd(self, _which) -> None:
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, json.dumps({"outcome": "finished", "evidence": {}}), "")

        config = AgentLauncherConfig(Path("/tmp/jesse"), profile="ats-lab")
        result = launch({"work_item_id": "JOB-1"}, config, runner=runner, environment={"SAFE": "1"})
        self.assertEqual(result["outcome"], "finished")
        command, kwargs = calls[0]
        self.assertEqual(command[0], "/bin/executor")
        self.assertEqual(command[1:3], ["-p", "ats-lab"])
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["cwd"], Path("/tmp/jesse"))
        self.assertEqual(kwargs["env"], {"SAFE": "1"})

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_command_restricts_toolsets_by_task_type(self, _which) -> None:
        config = AgentLauncherConfig(Path("/tmp/jesse"), profile="ats-lab")
        usage = Path("/tmp/usage.json")

        execution = build_command(
            config, "prompt", task_type="execute_batch", usage_path=usage,
        )
        analysis = build_command(
            config, "prompt", task_type="analyze_batch", usage_path=usage,
        )
        hpo = build_command(
            config, "prompt", task_type="analyze_hpo", usage_path=usage,
        )
        synthesis = build_command(
            config, "prompt", task_type="synthesize_batch", usage_path=usage,
        )

        self.assertIn("jesse", execution)
        self.assertNotIn("file", execution)
        for command in (analysis, hpo, synthesis):
            self.assertIn("context_engine", command)
            self.assertNotIn("jesse", command)
        self.assertEqual(execution[-2:], ["--usage-file", str(usage)])

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_launch_rejects_complete_session_payload_before_model_call(self, _which) -> None:
        called = False

        def runner(command, **kwargs):
            nonlocal called
            called = True

        result = launch(
            {
                "task_type": "analyze_batch",
                "executions": [{"session_payload": {"trades": [1, 2]}}],
            },
            AgentLauncherConfig(Path("/tmp")),
            runner=runner,
        )

        self.assertFalse(called)
        self.assertEqual(result["blocker_code"], "unsafe_model_context")

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_launch_writes_redacted_transport_telemetry(self, _which) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            telemetry = Path(tmp) / "transport.jsonl"

            def runner(command, **kwargs):
                usage_path = Path(command[command.index("--usage-file") + 1])
                usage_path.write_text(json.dumps({
                    "api_calls": 2,
                    "tokens": {
                        "input": 100,
                        "output": 20,
                        "cache_read": 80,
                    },
                    "model": "test-model",
                }))
                return subprocess.CompletedProcess(
                    command, 0, '{"outcome":"finished","evaluations":[]}', "",
                )

            result = launch(
                {"task_type": "analyze_batch", "executions": []},
                AgentLauncherConfig(
                    Path("/tmp"), telemetry_path=telemetry,
                ),
                runner=runner,
            )

            self.assertEqual(result["outcome"], "finished")
            report = json.loads(telemetry.read_text().strip())
            self.assertEqual(report["task_type"], "analyze_batch")
            self.assertGreater(report["request_bytes"], 0)
            self.assertEqual(
                report["response_bytes"],
                len('{"outcome":"finished","evaluations":[]}'.encode()),
            )
            self.assertEqual(report["model_call_count"], 2)
            self.assertEqual(report["input_tokens"], 100)
            self.assertEqual(report["cache_read_tokens"], 80)
            self.assertNotIn("prompt", report)
            self.assertNotIn("response", report)

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_telemetry_failure_does_not_fail_work(self, _which) -> None:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 0,
                '{"outcome":"finished","evidence":{"synthesis_requests":[]}}', "",
            )

        result = launch(
            {"task_type": "synthesize_batch", "context": {}},
            AgentLauncherConfig(
                Path("/tmp"), telemetry_path=Path("/dev/null/report.jsonl"),
            ),
            runner=runner,
        )

        self.assertEqual(result["outcome"], "finished")

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_timeout_becomes_retry(self, _which) -> None:
        def runner(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        result = launch({}, AgentLauncherConfig(Path("/tmp")), runner=runner)
        self.assertEqual(result["outcome"], "retry")
        self.assertEqual(result["blocker_code"], "executor_timeout")

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_analyzer_uses_bounded_request_timeout(self, _which) -> None:
        seen = {}

        def runner(command, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            return subprocess.CompletedProcess(
                command, 0, '{"outcome":"finished","evaluations":[]}', "",
            )

        result = launch(
            {
                "task_type": "analyze_batch",
                "analyzer_timeout_seconds": 720,
            },
            AgentLauncherConfig(Path("/tmp"), timeout_seconds=3600),
            runner=runner,
        )
        self.assertEqual(result["outcome"], "finished")
        self.assertEqual(seen["timeout"], 720)

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_strategy_preparation_uses_bounded_timeout(self, _which) -> None:
        seen = {}

        def runner(command, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            return subprocess.CompletedProcess(
                command, 0,
                '{"outcome":"finished","prepared_work_item_ids":["JOB-1"]}',
                "",
            )

        result = launch(
            {
                "task_type": "prepare_strategies",
                "requests": [{"work_item_id": "JOB-1"}],
            },
            AgentLauncherConfig(
                Path("/tmp"), timeout_seconds=3600,
                preparation_timeout_seconds=45,
            ),
            runner=runner,
        )

        self.assertEqual(result["outcome"], "finished")
        self.assertEqual(seen["timeout"], 45)

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_batch_execution_uses_bounded_timeout(self, _which) -> None:
        seen = {}

        def runner(command, **kwargs):
            seen["timeout"] = kwargs["timeout"]
            return subprocess.CompletedProcess(
                command, 0,
                '{"outcome":"finished","results":[]}',
                "",
            )

        result = launch(
            {"task_type": "execute_batch", "requests": []},
            AgentLauncherConfig(
                Path("/tmp"), timeout_seconds=3600,
                execution_timeout_seconds=75,
            ),
            runner=runner,
        )

        self.assertEqual(result["outcome"], "finished")
        self.assertEqual(seen["timeout"], 75)

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_analyzer_rejects_out_of_range_timeout(self, _which) -> None:
        result = launch(
            {
                "task_type": "analyze_batch",
                "analyzer_timeout_seconds": 599,
            },
            AgentLauncherConfig(Path("/tmp")),
        )
        self.assertEqual(result["blocker_code"], "invalid_analyzer_timeout")

    @patch("ats_lab.agent_launcher.shutil.which", return_value="/bin/executor")
    def test_non_json_output_becomes_retry(self, _which) -> None:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "not json", "")

        result = launch({}, AgentLauncherConfig(Path("/tmp")), runner=runner)
        self.assertEqual(result["blocker_code"], "invalid_executor_result")


if __name__ == "__main__":
    unittest.main()
