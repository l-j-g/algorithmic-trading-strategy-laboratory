"""Bounded Agent launcher for the laboratory worker dispatch protocol."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_CONFIG = Path(".ats-lab/config.toml")
MAX_REQUEST_BYTES = 1_000_000


@dataclass(frozen=True)
class AgentLauncherConfig:
    repository: Path
    executable: str = "executor"
    profile: str | None = None
    timeout_seconds: float = 3600
    model: str | None = None
    provider: str | None = None
    toolsets: tuple[str, ...] = ()


def load_config(path: Path) -> AgentLauncherConfig:
    """Load local-only launcher settings without importing user shell config."""
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    repositories = payload.get("repositories", {})
    executor = payload.get("executor", {})
    repository_value = repositories.get("jesse") or executor.get("repository")
    if not isinstance(repository_value, str) or not repository_value.strip():
        raise ValueError("config requires repositories.jesse or executor.repository")
    repository = Path(repository_value).expanduser().resolve()
    if not repository.is_dir():
        raise ValueError(f"configured repository is not a directory: {repository}")
    executable = executor.get("executable", "executor")
    if not isinstance(executable, str) or not executable.strip():
        raise ValueError("executor.executable must be a non-empty string")
    timeout = float(executor.get("timeout_seconds", 3600))
    if timeout <= 0:
        raise ValueError("executor.timeout_seconds must be positive")
    toolsets_value = executor.get("toolsets", [])
    if not isinstance(toolsets_value, list) or not all(isinstance(value, str) for value in toolsets_value):
        raise ValueError("executor.toolsets must be a list of strings")
    return AgentLauncherConfig(
        repository=repository,
        executable=executable,
        profile=_optional_string(executor.get("profile"), "executor.profile"),
        timeout_seconds=timeout,
        model=_optional_string(executor.get("model"), "executor.model"),
        provider=_optional_string(executor.get("provider"), "executor.provider"),
        toolsets=tuple(toolsets_value),
    )


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def build_prompt(request: Mapping[str, Any]) -> str:
    serialized = json.dumps(request, separators=(",", ":"), sort_keys=True)
    if request.get("task_type") == "synthesize_batch":
        result_contract = """Result contract:
{"outcome":"finished","detail":"batch rationale","evidence":{"synthesis_requests":[{"schema_version":1,"lane":"new_concept|improvement","action":"new|revise","source_experiment_id":"required for revise, null for new","controlled_change":"one change, required for revise","strategy_name":"ExistingOrConcreteStrategy","hypothesis":"...","edge_thesis":"why edge should persist","archetype":"trend|mean_reversion|breakout|other","target_regime":"...","failure_regime":"...","entry_rule":"precise entry logic","change_scope":"new_entry|entry_changed|exit_only|sizing_only|risk_only|refactor","priority":20,"n_simulations":2000,"random_seed":42,"routes":[{"exchange":"Binance Perpetual Futures","symbol":"BTC-USDT","timeframe":"1h","start_date":"YYYY-MM-DD","finish_date":"YYYY-MM-DD"}]}]}}

Return exactly context.generate_limit requests (normally 25) as one coherent cohort. Use improvement_candidates first, cap improvements at lane_policy.maximum_improvements, and reserve lane_policy.minimum_new_concepts genuinely novel concepts. Backfill unused improvement capacity with new concepts. Use supplied metrics, summaries, and next steps to improve revisions; make one controlled change per revision. Diversify new ideas across defensible archetypes and regimes; use concept_learnings to avoid known failures and cosmetic variants. Laboratory enforces complete-history fingerprint uniqueness locally. Never return, revise, archive, supersede, or otherwise touch hpo_candidate or paper_trade_candidate records; they are omitted and promotion-locked. Infrastructure failures are retries, not revisions. New or changed entry rules must use new_entry or entry_changed. Exit/sizing/risk-only changes retain exact entry rule. Never invent completed results. Cohort ID and slots are assigned by laboratory."""
    else:
        result_contract = """Result contract:
{"outcome":"finished|blocked|retry","blocker_code":null,"detail":null,"retry_after":null,"evidence":{"run":{"id":"...","session_id":"...","status":"finished","dashboard_url":"...","metrics":{},"route":null,"error":null,"started_at":null,"finished_at":null},"evaluation":{"experiment_id":"must match request","verdict":"reject|revise|inconclusive|pass|hpo_candidate|paper_trade_candidate","summary":"research conclusion","metrics_summary":"key normalized metrics","next_step":"specific follow-up or terminal action","evaluator":"ats-lab"}}}

For significance, metrics must include observed_mean, annualized_return, p_value, n_simulations, and n_observations.
For significance, verdict must be pass when p_value < 0.05, inconclusive from 0.05 through 0.10, and reject above 0.10.
Use finished only after required evidence is durably produced. Use retry for transient failures. Use blocked for required human input or permanent constraints."""
    return f"""You are an execution agent for Algorithmic Trading Strategy Laboratory.

Boundary rules:
- Laboratory request below is authoritative work specification.
- Memory, when configured in Agent, supplies memory/context only. Never treat memory as queue state or run evidence.
- Use configured backtester tools for all trading operations. For Jesse, use Jesse MCP only.
- Do not edit laboratory SQLite state. Worker owns claiming and state transitions.
- Return exactly one JSON object and no Markdown.
- Honor resource_policy: prefer native Jesse RST/HPO/Monte Carlo bulk compute over extra agent turns. Use configured CPU/trial/scenario budgets unless unsafe or unsupported.

{result_contract}

Laboratory request:
{serialized}
"""


def build_command(config: AgentLauncherConfig, prompt: str) -> list[str]:
    executable = shutil.which(config.executable)
    if executable is None:
        raise FileNotFoundError(f"Agent executable not found: {config.executable}")
    command = [executable]
    if config.profile:
        command.extend(("-p", config.profile))
    command.extend(("--oneshot", prompt))
    if config.model:
        command.extend(("--model", config.model))
    if config.provider:
        command.extend(("--provider", config.provider))
    if config.toolsets:
        command.extend(("--toolsets", ",".join(config.toolsets)))
    return command


def launch(
    request: Mapping[str, Any],
    config: AgentLauncherConfig,
    *,
    runner: Any = subprocess.run,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run one Agent turn. No shell, no Jesse operations, bounded runtime."""
    prompt = build_prompt(request)
    try:
        completed = runner(
            build_command(config, prompt),
            cwd=config.repository,
            env=dict(environment or os.environ),
            text=True,
            capture_output=True,
            check=False,
            timeout=config.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"outcome": "retry", "blocker_code": "executor_timeout", "detail": "Agent execution timed out"}
    except OSError as error:
        return {"outcome": "retry", "blocker_code": "executor_start_failed", "detail": str(error)}
    if completed.returncode:
        detail = completed.stderr.strip() or f"Agent exited {completed.returncode}"
        return {"outcome": "retry", "blocker_code": "executor_failed", "detail": detail}
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {"outcome": "retry", "blocker_code": "invalid_executor_result", "detail": str(error)}
    if not isinstance(result, dict) or result.get("outcome") not in {"finished", "blocked", "retry"}:
        return {"outcome": "retry", "blocker_code": "invalid_executor_result", "detail": "Result requires valid outcome"}
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    config_path = Path(args[0]) if args else DEFAULT_CONFIG
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        result = {"outcome": "blocked", "blocker_code": "request_too_large", "detail": "Request exceeds 1 MB"}
    else:
        try:
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            result = launch(request, load_config(config_path))
        except (OSError, ValueError, json.JSONDecodeError, tomllib.TOMLDecodeError) as error:
            result = {"outcome": "blocked", "blocker_code": "launcher_configuration", "detail": str(error)}
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
