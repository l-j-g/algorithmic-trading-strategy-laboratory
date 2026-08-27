"""Short idempotent CLI entry points for Agent/Memory orchestration."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sqlite3
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .audit import build_audit, render_markdown
from .activity_log import ActivityFollower, load_activity_log_config
from .database import WorkflowDatabase
from .hpo_routes import HpoRoutePlanner, default_hpo_routes, render_hpo_route_plan
from .hpo import import_jesse_session_export, import_optuna_study
from .direct_mcp_executor import (
    DirectMcpDispatcher,
    McpClient,
    load_direct_execution_config,
)
from .dashboard import serve as serve_dashboard
from .web_api import serve as serve_backend, serve_web
from .inventory import build_inventory, render_markdown as render_inventory
from .legacy_import import LegacyImporter
from .loop_control import SupervisorLoopControl
from .contracts import evaluation_from_payload, experiment_from_payload, load_json, work_item_from_payload
from .console import (
    distinct_candidate_evidence,
    monitor_snapshot,
    render_analyzer,
    render_control,
    render_evidence,
    render_hpo_readiness,
    render_hpo_detail,
    render_hpo_studies,
    render_monitor,
    render_stage_timings,
    render_table,
    run_console,
    watch_monitor,
)
from .cli_ux import (
    ROOT_HELP,
    next_guidance,
    render_doctor,
    render_guidance,
    render_home,
    render_memory_init,
    render_memory_status,
    render_memory_sync,
)
from .correctness_recovery import (
    backfill_aggregate_route_coverage,
    classify_recovery_candidates,
    recover_executor_infrastructure_failures,
    recover_orphaned_replacement_reservations,
    recover_partial_batch_retries,
    recover_zombie_execution_sessions,
)
from .models import WorkState
from .reconcile import apply_reconciliation, build_reconciliation, normalize_unattempted_blockers
from .resources import load_resource_policy
from .research_memory import (
    MemoryProviderConfig,
    MemoryResearchAdapter,
    backfill_memory_outbox,
    initialize_research_memory,
    memory_status,
    sync_memory_outbox,
)
from .telemetry_rollup import TelemetryRollup
from .sanitize import apply_sanitize_plan, build_sanitize_plan
from .synthesis import synthesis_request_from_file, synthesize
from .status import hpo_detail_snapshot, operator_status
from .stack_preflight import StackPreflight
from .supervisor import BatchSupervisor
from .terminal_ui import run_tui
from .worker import CommandDispatcher, Worker


def emit(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _read_hpo_classifications(
    repo: Path, source: Path | None,
) -> dict[int, dict] | None:
    if source is None:
        return None
    path = source if source.is_absolute() else repo / source
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("classifications file must contain an object")
    classifications: dict[int, dict] = {}
    for number, value in raw.items():
        if not isinstance(value, dict):
            raise ValueError(
                f"classification for trial {number} must be an object"
            )
        classifications[int(number)] = value
    return classifications


def emit_progress(value: object) -> None:
    """Emit compact continuous-worker progress; full state stays queryable."""
    if not isinstance(value, dict):
        emit(value)
        return
    compact = {
        key: item for key, item in value.items()
        if key not in {
            "operator", "synthesis", "cohorts", "evaluated",
            "execution_results",
        }
    }
    operator = value.get("operator")
    if isinstance(operator, dict):
        compact["queue"] = operator.get("work_states")
        compact["next_action"] = operator.get("next_action")
        hpo = operator.get("hpo")
        if isinstance(hpo, dict):
            analyzer = hpo.get("analyzer")
            if isinstance(analyzer, dict):
                compact["analyzer_state"] = analyzer.get("state")
    synthesis = value.get("synthesis")
    if isinstance(synthesis, dict):
        compact["synthesis"] = {
            "generated": len(synthesis.get("generated") or []),
            "rejected": len(synthesis.get("rejected") or []),
            "submitted": synthesis.get("submitted"),
        }
    cohorts = value.get("cohorts")
    if isinstance(cohorts, list):
        compact["cohorts"] = [
            {
                key: cohort.get(key)
                for key in (
                    "status", "cohort_id", "attempt", "payload_bytes",
                )
            } | {"evaluated": len(cohort.get("evaluated") or [])}
            for cohort in cohorts if isinstance(cohort, dict)
        ]
    for field in ("evaluated", "execution_results"):
        rows = value.get(field)
        if isinstance(rows, list):
            compact[f"{field}_count"] = len(rows)
    emit(compact)


class AtsLabArgumentParser(argparse.ArgumentParser):
    """Keep root help useful while preserving detailed command help."""

    def format_help(self) -> str:
        if self.prog == "ats-lab":
            return ROOT_HELP
        return super().format_help()


def discover_lab_repo(start: Path, fallback: Path | None = None) -> Path:
    """Return the nearest parent containing ATS Lab configuration."""
    resolved = start.resolve()
    found = next(
        (
            candidate for candidate in (resolved, *resolved.parents)
            if (candidate / ".ats-lab" / "config.toml").is_file()
        ), None,
    )
    if found is not None:
        return found
    if fallback is not None and (
        fallback / ".ats-lab" / "config.toml"
    ).is_file():
        return fallback.resolve()
    return resolved


def build_stack_preflight(repo: Path) -> StackPreflight:
    config = load_direct_execution_config(repo / ".ats-lab" / "config.toml")
    return StackPreflight(
        dashboard_url=config.dashboard_api_base_url,
        mcp_url=config.mcp_url,
        postgres_container=os.environ.get(
            "ATS_LAB_JESSE_POSTGRES_CONTAINER", "postgres",
        ),
        postgres_user=os.environ.get(
            "ATS_LAB_JESSE_POSTGRES_USER", "jesse_user",
        ),
        postgres_database=os.environ.get(
            "ATS_LAB_JESSE_POSTGRES_DATABASE", "jesse_db",
        ),
        memory_health_url=os.environ.get(
            "ATS_LAB_MEMORY_HEALTH_URL", "http://127.0.0.1:18000/health",
        ),
    )


def _memory_adapter() -> MemoryResearchAdapter:
    return MemoryResearchAdapter(MemoryProviderConfig(base_url=os.environ.get(
        "ATS_LAB_MEMORY_URL", "http://127.0.0.1:18000",
    )))


def _add_memory_format_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=("table", "json"), default="table")


def _present_memory_result(
    result: object, args_format: str,
    renderer: Callable[[object], str],
) -> None:
    if args_format == "json":
        emit(result)
    else:
        print(renderer(result))


class CommandContext:
    """Shared dependencies for one dispatched CLI command."""

    def __init__(
        self,
        parser: AtsLabArgumentParser,
        args: argparse.Namespace,
        repo: Path,
        database: WorkflowDatabase,
        database_path: Path,
    ) -> None:
        self.parser = parser
        self.args = args
        self.repo = repo
        self.database = database
        self.database_path = database_path


def build_parser() -> AtsLabArgumentParser:
    parser = AtsLabArgumentParser(prog="ats-lab", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path, default=Path(".ats-lab/laboratory.sqlite3"))
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("help", help="Show curated daily-operation help.")
    next_parser = sub.add_parser("next", help="Show one recommended next action.")
    next_parser.add_argument("--format", choices=("table", "json"), default="table")
    doctor = sub.add_parser(
        "doctor", help="Check infrastructure, workflow, memory, and next action."
    )
    doctor.add_argument("--format", choices=("table", "json"), default="table")
    sub.add_parser("init")
    sub.add_parser("migrate-legacy")
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--markdown", type=Path)
    queue_parser = sub.add_parser("queue")
    queue_parser.add_argument("--state")
    queue_parser.add_argument("--format", choices=("table", "json"), default="table")
    sub.add_parser("synthesis-status")
    sub.add_parser("memory-status", help="Show ATS research-memory outbox state.")
    memory = sub.add_parser(
        "memory", help="Initialize, inspect, or synchronize advisory research memory."
    )
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_init = memory_sub.add_parser(
        "init",
        help="Backfill all safe canonical findings and deliver them to Memory.",
    )
    memory_init.add_argument("--dry-run", action="store_true")
    memory_init.add_argument("--batch-size", type=int, default=100)
    memory_init.add_argument("--delivery-limit", type=int, default=100)
    _add_memory_format_argument(memory_init)
    memory_status_nested = memory_sub.add_parser(
        "status", help="Show research-memory readiness."
    )
    _add_memory_format_argument(memory_status_nested)
    memory_sync_nested = memory_sub.add_parser(
        "sync", help="Deliver currently queued research memory to Memory."
    )
    memory_sync_nested.add_argument("--dry-run", action="store_true")
    memory_sync_nested.add_argument("--limit", type=int, default=100)
    _add_memory_format_argument(memory_sync_nested)
    memory_sync = sub.add_parser(
        "memory-sync", help="Preview or dispatch bounded research-memory outbox records."
    )
    memory_mode = memory_sync.add_mutually_exclusive_group(required=True)
    memory_mode.add_argument("--dry-run", action="store_true")
    memory_mode.add_argument("--apply", action="store_true")
    memory_sync.add_argument("--limit", type=int, default=25)
    memory_backfill = sub.add_parser(
        "memory-backfill",
        help="Queue bounded historical learnings from canonical completed evidence.",
    )
    memory_backfill_mode = memory_backfill.add_mutually_exclusive_group()
    memory_backfill_mode.add_argument("--dry-run", action="store_true")
    memory_backfill_mode.add_argument("--apply", action="store_true")
    memory_backfill.add_argument("--batch-size", type=int, default=100)
    sub.add_parser(
        "preflight",
        help="Check Docker, Jesse dashboard/MCP, and Memory before execution.",
    )
    status_parser = sub.add_parser(
        "status", help="Show compact workflow health and recommended next action."
    )
    status_parser.add_argument("--format", choices=("table", "json"), default="table")
    start = sub.add_parser(
        "start", help="Start or resume research and follow its activity stream."
    )
    start.add_argument("--idle-sleep", type=float, default=30.0)
    start.add_argument("--retry-delay", type=float, default=60.0)
    start.add_argument("--interval", type=float, default=1.0)
    monitor = sub.add_parser("monitor", help="Show human-readable terminal progress.")
    monitor.add_argument("--watch", action="store_true")
    monitor.add_argument("--interval", type=float, default=5.0)
    tui = sub.add_parser(
        "tui", help="Open full-screen color operator UI with keyboard navigation."
    )
    tui.add_argument("--interval", type=float, default=1.0)
    loop = sub.add_parser(
        "loop", help="Start, pause, stop, or inspect the supervisor process."
    )
    loop_sub = loop.add_subparsers(dest="loop_command", required=True)
    loop_start = loop_sub.add_parser("start", help="Start or resume the research loop.")
    loop_start.add_argument("--idle-sleep", type=float, default=30.0)
    loop_start.add_argument("--retry-delay", type=float, default=60.0)
    loop_start.add_argument("--format", choices=("table", "json"), default="table")
    for name in ("status", "pause", "stop"):
        command = loop_sub.add_parser(name)
        command.add_argument("--format", choices=("table", "json"), default="table")
    control = sub.add_parser("control", help="Pause, resume, or gracefully stop supervisor.")
    control.add_argument("action", choices=("status", "pause", "resume", "stop"))
    control.add_argument("--format", choices=("table", "json"), default="table")
    console = sub.add_parser("console", help="Open interactive terminal control console.")
    console.add_argument("--interval", type=float, default=5.0)
    recover_claims = sub.add_parser(
        "recover-claims", help="Preview stale running claims with no durable run evidence."
    )
    recover_claims.add_argument("--stale-after-hours", type=float, default=2.0)
    recover_claims.add_argument("--apply", action="store_true")
    resolve_blocker = sub.add_parser(
        "resolve-blocker",
        help="Reopen one fixed blocker with durable resolution evidence.",
    )
    resolve_blocker.add_argument("work_item_id")
    resolve_blocker.add_argument("--code", required=True)
    resolve_blocker.add_argument("--detail", required=True)
    resolve_blocker.add_argument("--evidence", action="append", default=[])
    requeue_evaluation = sub.add_parser(
        "requeue-evaluation",
        help="Reanalyze durable finished-run metrics without rerunning execution.",
    )
    requeue_evaluation.add_argument("work_item_id")
    requeue_evaluation.add_argument(
        "--worker", default=os.environ.get("ATS_LAB_WORKER_ID", "ats-lab-supervisor")
    )
    requeue_evaluation.add_argument("--batch")
    requeue_evaluation.add_argument("--reason", required=True)
    candidates = sub.add_parser("candidates")
    candidates.add_argument("--verdict")
    candidates.add_argument("--format", choices=("table", "json"), default="table")
    evidence = sub.add_parser(
        "evidence", help="Show standardized candidate evidence."
    )
    evidence.add_argument("--strategy")
    evidence.add_argument("--stage")
    evidence.add_argument("--verdict")
    evidence.add_argument("--symbol")
    evidence.add_argument("--timeframe")
    evidence.add_argument(
        "--split", choices=("train", "holdout", "oos", "rolling")
    )
    evidence.add_argument(
        "--rank",
        choices=(
            "net_profit_percentage", "max_drawdown_percentage",
            "sharpe_ratio", "sortino_ratio", "calmar_ratio",
            "profit_factor", "win_rate", "trade_count", "expectancy",
        ),
    )
    evidence.add_argument("--limit", type=int, default=20)
    evidence.add_argument("--format", choices=("table", "json"), default="table")
    diagnostic = sub.add_parser(
        "diagnostic-export", help="Export raw evidence for one run."
    )
    diagnostic.add_argument("run_id")
    diagnostic_trial = sub.add_parser(
        "diagnostic-hpo-trial",
        help="Export raw optimizer parameters for one HPO trial.",
    )
    diagnostic_trial.add_argument("study_id")
    diagnostic_trial.add_argument("trial_number", type=int)
    hpo = sub.add_parser(
        "hpo", help="Show unified HPO lifecycle and progress."
    )
    hpo.add_argument(
        "--state",
        choices=(
            "hpo_candidate", "hpo_scheduled", "hpo_running",
            "hpo_analysis", "validation", "paper_trade_candidate",
            "revise", "reject",
        ),
    )
    hpo.add_argument("--strategy")
    hpo.add_argument("--limit", type=int, default=100)
    hpo.add_argument(
        "--doctor", action="store_true",
        help="Show route readiness, validation jobs, and the next HPO action.",
    )
    hpo.add_argument("--format", choices=("table", "json"), default="table")
    hpo_detail = sub.add_parser(
        "hpo-detail", help="Show selected trials, validation, and timings."
    )
    hpo_detail.add_argument("study_id")
    hpo_detail.add_argument(
        "--format", choices=("table", "json"), default="table"
    )
    hpo_route_plan = sub.add_parser(
        "hpo-route-plan",
        help="Show required HPO/OOS/rolling route readiness without changing state.",
    )
    hpo_route_plan.add_argument("study_id")
    hpo_route_plan.add_argument(
        "--format", choices=("table", "json"), default="table",
    )
    hpo_defaults = sub.add_parser(
        "hpo-defaults",
        help="Show or apply disjoint bootstrap routes for untouched HPO studies.",
    )
    hpo_defaults.add_argument(
        "study_id", nargs="?",
        help="One scheduled study; omit to apply/show all eligible studies.",
    )
    hpo_defaults.add_argument(
        "--apply", action="store_true",
        help="Persist defaults and release matching scheduled HPO work.",
    )
    hpo_defaults.add_argument("--format", choices=("table", "json"), default="table")
    timings = sub.add_parser(
        "timings", help="Show lifecycle stage durations."
    )
    timings.add_argument("--job")
    timings.add_argument("--limit", type=int, default=100)
    timings.add_argument("--format", choices=("table", "json"), default="table")
    telemetry = sub.add_parser(
        "telemetry", help="Summarize privacy-safe Agent transport telemetry."
    )
    telemetry.add_argument("--path", type=Path)
    telemetry.add_argument("--since-hours", type=float, default=24)
    telemetry.add_argument("--format", choices=("table", "json"), default="table")
    analyzer = sub.add_parser(
        "analyzer", help="Show current HPO analyzer state."
    )
    analyzer.add_argument("--format", choices=("table", "json"), default="table")
    requeue_hpo = sub.add_parser(
        "requeue-hpo-analysis",
        help="Reopen one terminal HPO analyzer job after fixing its blocker.",
    )
    requeue_hpo.add_argument("job_id")
    requeue_hpo.add_argument("--reason", required=True)
    requeue_hpo.add_argument(
        "--operator", default=os.environ.get("USER", "operator")
    )
    requeue_hpo_execution = sub.add_parser(
        "requeue-hpo-execution",
        help="Reopen a trial-less HPO optimizer after its provider is repaired.",
    )
    requeue_hpo_execution.add_argument("study_id")
    requeue_hpo_execution.add_argument("--reason", required=True)
    requeue_hpo_execution.add_argument(
        "--operator", default=os.environ.get("USER", "operator")
    )
    validation_routes = sub.add_parser(
        "configure-hpo-validation-routes",
        help=(
            "Supply canonical HPO training and/or OOS/rolling routes; "
            "release only matching work."
        ),
    )
    validation_routes.add_argument("study_id")
    validation_routes.add_argument("--file", type=Path, required=True)
    validation_routes.add_argument(
        "--operator", default=os.environ.get("USER", "operator")
    )
    hpo_import = sub.add_parser(
        "hpo-import",
        help="Attach completed Optuna trials to a parked HPO study and resume analysis.",
    )
    hpo_import.add_argument("study_id", help="Existing ATS HPO study to resume.")
    hpo_import.add_argument("--file", type=Path, required=True, help="Optuna SQLite database.")
    hpo_import.add_argument("--study-name", required=True, help="Exact Optuna study name.")
    hpo_import.add_argument(
        "--classifications", type=Path,
        help="Optional JSON object mapping trial numbers to classifications.",
    )
    hpo_import.add_argument("--format", choices=("table", "json"), default="table")
    jesse_hpo_import = sub.add_parser(
        "hpo-import-jesse-session",
        help=(
            "Attach a complete exported Jesse optimization session to a "
            "parked HPO study."
        ),
    )
    jesse_hpo_import.add_argument(
        "study_id", help="Existing ATS HPO study to resume."
    )
    jesse_hpo_import.add_argument(
        "--file", type=Path, required=True,
        help="Versioned complete Jesse optimization-session JSON export.",
    )
    jesse_hpo_import.add_argument(
        "--classifications", type=Path,
        help="Optional JSON object mapping trial numbers to classifications.",
    )
    jesse_hpo_import.add_argument(
        "--format", choices=("table", "json"), default="table",
    )
    claim = sub.add_parser("claim")
    claim.add_argument("--worker", default=os.environ.get("ATS_LAB_WORKER_ID", "ats-lab-worker"))
    inventory_parser = sub.add_parser("inventory")
    inventory_parser.add_argument("--markdown", type=Path)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--file", type=Path, required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--file", type=Path, required=True)
    finish = sub.add_parser("finish")
    finish.add_argument("work_item_id")
    block = sub.add_parser("block")
    block.add_argument("work_item_id")
    block.add_argument("--code", required=True)
    block.add_argument("--detail", required=True)
    retry = sub.add_parser("retry")
    retry.add_argument("work_item_id")
    retry.add_argument("--after", required=True, help="ISO-8601 retry time.")
    reconcile = sub.add_parser("reconcile", help="Classify imported queue state; dry-run unless --apply.")
    reconcile.add_argument("--stale-after-hours", type=float, default=24.0)
    reconcile.add_argument("--apply", action="store_true")
    normalize = sub.add_parser("normalize-blockers", help="Return never-attempted legacy blockers to scheduled backlog.")
    normalize.add_argument("--apply", action="store_true")
    route_backfill = sub.add_parser(
        "backfill-route-coverage",
        help="Persist aggregate requested-route coverage proven by finished Jesse sessions.",
    )
    route_backfill.add_argument("--apply", action="store_true")
    data_route_repair = sub.add_parser(
        "repair-data-routes",
        help="Repair auxiliary Jesse candle routes and reopen one unreconciled work item.",
    )
    data_route_repair.add_argument("work_item_id")
    data_route_repair.add_argument(
        "--route", action="append", required=True, dest="routes",
        help='Auxiliary route JSON, repeat for multiple routes.',
    )
    data_route_repair.add_argument("--reason", required=True)
    data_route_repair.add_argument("--apply", action="store_true")
    partial_recovery = sub.add_parser(
        "recover-partial-batch-retries",
        help="Reopen explicit jobs charged by the known batch-wide retry defect.",
    )
    partial_recovery.add_argument(
        "--work-item", action="append", required=True,
        dest="work_items",
    )
    partial_recovery.add_argument("--apply", action="store_true")
    executor_recovery = sub.add_parser(
        "recover-executor-infrastructure",
        help="Replay durable evidence and requeue unexecuted Agent transport failures.",
    )
    executor_recovery.add_argument("--apply", action="store_true")
    executor_recovery.add_argument("--worker", default="ats-lab-supervisor")
    replacement_recovery = sub.add_parser(
        "recover-orphaned-replacements",
        help="Clear replacement reservations that never persisted a session id.",
    )
    replacement_recovery.add_argument("--apply", action="store_true")
    zombie_recovery = sub.add_parser(
        "recover-zombie-sessions",
        help="Inspect and recover explicit evidence-free Jesse session checkpoints.",
    )
    zombie_recovery.add_argument(
        "--session-id", action="append", required=True, dest="session_ids",
    )
    zombie_recovery.add_argument("--apply", action="store_true")
    sub.add_parser(
        "recovery-audit",
        help="Classify all retry/blocker candidates using persisted and live session evidence.",
    )
    sanitize = sub.add_parser("sanitize", help="Evaluate terminal evidence and delete dead active queue items.")
    sanitize.add_argument("--apply", action="store_true")
    synthesis = sub.add_parser("synthesize", help="Create gated jobs from a typed research idea.")
    synthesis.add_argument("--file", type=Path, required=True)
    worker = sub.add_parser("worker", help="Compatibility-only single-item worker.")
    worker.add_argument("--worker", default=os.environ.get("ATS_LAB_WORKER_ID", "ats-lab-worker"))
    worker.add_argument("--dispatch-command", default=os.environ.get("ATS_LAB_DISPATCH_COMMAND"))
    worker.add_argument("--continuous", action="store_true")
    worker.add_argument("--idle-sleep", type=float, default=30.0)
    worker.add_argument("--retry-delay", type=float, default=60.0)
    worker.add_argument("--max-attempts", type=int, default=5)
    worker.add_argument("--max-items", type=int)
    worker.add_argument(
        "--no-idle-synthesis", action="store_true",
        help="Disable Agent replenishment when unresolved chains reach the low watermark.",
    )
    supervisor = sub.add_parser(
        "supervisor",
        help="Canonical batch execution, isolated analysis, and synthesis loop.",
    )
    supervisor.add_argument("--worker", default=os.environ.get("ATS_LAB_WORKER_ID", "ats-lab-supervisor"))
    supervisor.add_argument("--dispatch-command", default=os.environ.get("ATS_LAB_DISPATCH_COMMAND"))
    supervisor.add_argument("--plan", action="store_true", help="Read-only health and policy check.")
    supervisor.add_argument("--continuous", action="store_true")
    supervisor.add_argument("--idle-sleep", type=float, default=30.0)
    supervisor.add_argument("--retry-delay", type=float, default=60.0)
    supervisor.add_argument("--max-attempts", type=int, default=5)
    supervisor.add_argument("--max-rounds", type=int)
    dashboard = sub.add_parser("dashboard", help="Serve the local read-only operator dashboard.")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8799)
    backend = sub.add_parser(
        "backend", help="Serve the local read-only backend API for CLI/web clients."
    )
    backend.add_argument("--host", default="127.0.0.1")
    backend.add_argument("--port", type=int, default=8766)
    backend.add_argument("--claim-timeout-seconds", type=int, default=7200)
    web = sub.add_parser(
        "web", help="Serve the static Control Room and read-only backend API."
    )
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--claim-timeout-seconds", type=int, default=7200)
    return parser


def _run_home(context: CommandContext) -> int:
    database = context.database
    database.initialize()
    snapshot = monitor_snapshot(database)
    print(render_home(snapshot, memory_status(database)))
    return 0


def _run_next(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    guidance = next_guidance(
        monitor_snapshot(database), memory_status(database),
    )
    if args.format == "json":
        emit(guidance)
    else:
        print(render_guidance(guidance))
    return 0


def _run_doctor(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    preflight = build_stack_preflight(repo).check()
    snapshot = monitor_snapshot(database)
    memory = memory_status(database)
    if args.format == "json":
        emit({
            "healthy": bool(preflight.get("healthy"))
            and bool(snapshot.get("healthy")),
            "preflight": preflight,
            "workflow": operator_status(database),
            "memory": memory,
            "next": next_guidance(snapshot, memory),
        })
    else:
        print(render_doctor(preflight, snapshot, memory))
    return 0 if preflight.get("healthy") and snapshot.get("healthy") else 2


def _run_init(context: CommandContext) -> int:
    database = context.database
    database.initialize()
    emit({"database": str(context.database_path), "status": "initialized"})
    return 0


def _run_preflight(context: CommandContext) -> int:
    result = build_stack_preflight(context.repo).check()
    emit(result)
    return 0 if result["healthy"] else 2


def _run_backend(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.claim_timeout_seconds <= 0:
        parser.error("--claim-timeout-seconds must be positive")
    serve_backend(
        context.database,
        host=args.host,
        port=args.port,
        claim_timeout_seconds=args.claim_timeout_seconds,
    )
    return 0


def _run_web(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.claim_timeout_seconds <= 0:
        parser.error("--claim-timeout-seconds must be positive")
    serve_web(
        context.database,
        context.repo,
        host=args.host,
        port=args.port,
        claim_timeout_seconds=args.claim_timeout_seconds,
    )
    return 0


def _run_memory_status(context: CommandContext) -> int:
    database = context.database
    database.initialize()
    emit(memory_status(database))
    return 0


def _run_memory(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    database.initialize()
    if args.memory_command == "status":
        result = memory_status(database)
        _present_memory_result(result, args.format, render_memory_status)
    elif args.memory_command == "init":
        if args.batch_size < 1 or args.batch_size > 1000:
            parser.error("memory init --batch-size must be between 1 and 1000")
        if args.delivery_limit < 1 or args.delivery_limit > 100:
            parser.error("memory init --delivery-limit must be between 1 and 100")
        adapter = None if args.dry_run else _memory_adapter()

        def memory_progress(item: dict) -> None:
            fields = " ".join(
                f"{key}={value}" for key, value in item.items()
                if key != "phase"
            )
            print(f"MEMORY {item['phase']} {fields}", flush=True)

        result = initialize_research_memory(
            database, adapter, apply=not args.dry_run,
            batch_size=args.batch_size, sync_limit=args.delivery_limit,
            progress=(
                memory_progress
                if not args.dry_run and args.format == "table"
                else None
            ),
        )
        _present_memory_result(result, args.format, render_memory_init)
    elif args.memory_command == "sync":
        if args.limit < 1 or args.limit > 100:
            parser.error("memory sync --limit must be between 1 and 100")
        result = sync_memory_outbox(
            database, _memory_adapter(), apply=not args.dry_run,
            limit=args.limit,
        )
        _present_memory_result(result, args.format, render_memory_sync)
    return 0


def _run_memory_sync(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if args.limit < 1 or args.limit > 100:
        parser.error("memory-sync --limit must be between 1 and 100")
    database.initialize()
    emit(sync_memory_outbox(
        database, _memory_adapter(), apply=args.apply, limit=args.limit,
    ))
    return 0


def _run_memory_backfill(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if args.batch_size < 1 or args.batch_size > 1000:
        parser.error("memory-backfill --batch-size must be between 1 and 1000")
    database.initialize()
    emit(backfill_memory_outbox(
        database, apply=args.apply, batch_size=args.batch_size,
    ))
    return 0


def _run_migrate_legacy(context: CommandContext) -> int:
    emit(LegacyImporter(context.repo, context.database).import_all())
    return 0


def _run_audit(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    result = build_audit(database)
    if args.markdown:
        output = args.markdown if args.markdown.is_absolute() else repo / args.markdown
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_markdown(result))
        result["markdown"] = str(output)
    emit(result)
    return 0


def _run_queue(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    query = "SELECT * FROM active_queue"
    parameters: tuple = ()
    if args.state:
        query += " WHERE state = ?"
        parameters = (args.state,)
    query += " ORDER BY priority, created_at, id"
    rows = database.rows(query, parameters)
    if args.format == "json":
        emit(rows)
    else:
        print(render_table(rows, (
            ("state", "state", 15),
            ("priority", "prio", 5),
            ("strategy", "strategy", 26),
            ("id", "job", 31),
            ("attempts", "tries", 5),
            ("blocker_code", "blocker", 24),
            ("retry_after", "retry after", 20),
            ("blocker_detail", "detail", 36),
        )))
    return 0


def _run_synthesis_status(context: CommandContext) -> int:
    database = context.database
    database.initialize()
    emit(database.synthesis_status())
    return 0


def _run_status(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    if args.format == "json":
        emit(operator_status(database))
    else:
        print(render_monitor(
            monitor_snapshot(database), color=sys.stdout.isatty() and not os.environ.get("NO_COLOR"),
        ))
    return 0


def _run_monitor(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if args.interval <= 0:
        parser.error("--interval must be positive")
    database.initialize()
    if not args.watch:
        print(render_monitor(
            monitor_snapshot(database), color=sys.stdout.isatty() and not os.environ.get("NO_COLOR"),
        ))
    else:
        try:
            watch_monitor(database, interval=args.interval)
        except KeyboardInterrupt:
            return 130
    return 0


def _run_start(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    if args.idle_sleep < 0 or args.retry_delay < 0:
        parser.error("start sleep values must be non-negative")
    if args.interval <= 0:
        parser.error("start --interval must be positive")
    database.initialize()
    cursor = database.latest_event_id()
    lifecycle = SupervisorLoopControl(
        database, repo,
        idle_sleep=args.idle_sleep,
        retry_delay=args.retry_delay,
    )
    result = lifecycle.start()
    runtime = database.supervisor_runtime_status() or {}
    if result.state == "already_running":
        database.record_event(
            "supervisor", "cli", "research_attached",
            {"stage": "starting", "process_id": result.process_id},
        )
    follower = ActivityFollower(
        database,
        output=sys.stdout,
        config=load_activity_log_config(repo),
        cursor=cursor,
        started_at=(
            runtime.get("started_at")
            if result.state == "already_running" else None
        ),
        interval=args.interval,
    )
    try:
        follower.run()
    except KeyboardInterrupt:
        lifecycle.stop()
        database.record_event(
            "supervisor", "cli", "stop_requested",
            {"stage": "stopping", "reason": "keyboard_interrupt"},
        )
        try:
            follower.run()
        except KeyboardInterrupt:
            if sys.stdout.isatty():
                print()
            print(
                "Stop requested; supervisor continues finishing current work.",
                file=sys.stderr,
            )
            return 130
    return 0


def _run_tui(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    if args.interval <= 0:
        parser.error("tui --interval must be positive")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("tui requires an interactive terminal; use ats-lab monitor")
    database.initialize()
    try:
        return run_tui(database, repo=repo, interval=args.interval)
    except KeyboardInterrupt:
        return 130


def _run_loop(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    database.initialize()
    idle_sleep = getattr(args, "idle_sleep", 30.0)
    retry_delay = getattr(args, "retry_delay", 60.0)
    if idle_sleep < 0 or retry_delay < 0:
        parser.error("loop sleep values must be non-negative")
    lifecycle = SupervisorLoopControl(
        database, repo,
        idle_sleep=idle_sleep,
        retry_delay=retry_delay,
    )
    result = {
        "start": lifecycle.start,
        "status": lifecycle.status,
        "pause": lifecycle.pause,
        "stop": lifecycle.stop,
    }[args.loop_command]().to_dict()
    if args.format == "json":
        emit(result)
    else:
        print(
            f"LOOP {str(result['state']).upper()}  "
            f"pid={result['process_id'] or '—'}  "
            f"phase={result['phase']}  control={result['control']}"
        )
        if result["repaired_retry_schedules"]:
            print(
                "REPAIRED retry_schedules="
                f"{result['repaired_retry_schedules']}"
            )
    return 0


def _run_control(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    if args.action == "status":
        state = database.control_status()
    else:
        desired_state = {
            "pause": "paused",
            "resume": "running",
            "stop": "stop_requested",
        }[args.action]
        state = database.set_control_state(
            desired_state, updated_by=f"cli:{args.action}",
        )
    runtime = database.supervisor_runtime_status()
    if args.format == "json":
        emit({"control": state, "supervisor": runtime})
    else:
        print(render_control(state, runtime))
    return 0


def _run_console(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if args.interval <= 0:
        parser.error("--interval must be positive")
    database.initialize()
    try:
        return run_console(database, interval=args.interval)
    except KeyboardInterrupt:
        print("\nconsole stopped")
        return 130


def _run_recover_claims(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if args.stale_after_hours <= 0:
        parser.error("--stale-after-hours must be positive")
    database.initialize()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=args.stale_after_hours)
    ).isoformat().replace("+00:00", "Z")
    emit(database.recover_stale_unexecuted_claims(cutoff, apply=args.apply))
    return 0


def _run_resolve_blocker(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    emit(database.resolve_blocked_work_item(
        args.work_item_id,
        resolution_code=args.code,
        detail=args.detail,
        evidence_ids=args.evidence,
    ))
    return 0


def _run_requeue_evaluation(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    emit(database.requeue_finished_evaluation(
        args.work_item_id,
        worker_id=args.worker,
        reason=args.reason,
        batch_id=args.batch,
    ))
    return 0


def _run_candidates(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    filters = {}
    if args.verdict:
        filters["verdict"] = args.verdict.replace("-", "_")
    evidence_rows = database.query_normalized_evidence(
        filters=filters, limit=500,
    )
    evidence_rows = distinct_candidate_evidence(evidence_rows)
    if args.format == "json":
        emit([item.to_dict() for item in evidence_rows])
    else:
        print(render_evidence(evidence_rows))
    return 0


def _run_evidence(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if args.limit < 1 or args.limit > 5000:
        parser.error("--limit must be between 1 and 5000")
    database.initialize()
    names = {
        "strategy": "strategy", "stage": "lifecycle_stage",
        "verdict": "verdict", "symbol": "symbol",
        "timeframe": "timeframe", "split": "evidence_split",
    }
    filters = {
        field: getattr(args, argument)
        for argument, field in names.items()
        if getattr(args, argument)
    }
    evidence_rows = database.query_normalized_evidence(
        filters=filters, limit=args.limit,
    )
    if args.rank:
        reverse = args.rank != "max_drawdown_percentage"
        present = [
            item for item in evidence_rows
            if getattr(item, args.rank) is not None
        ]
        missing = [
            item for item in evidence_rows
            if getattr(item, args.rank) is None
        ]
        present.sort(
            key=lambda item: (
                abs(getattr(item, args.rank))
                if args.rank == "max_drawdown_percentage"
                else getattr(item, args.rank)
            ),
            reverse=reverse,
        )
        evidence_rows = present + missing
    if args.format == "json":
        emit([item.to_dict() for item in evidence_rows])
    else:
        print(render_evidence(evidence_rows))
    return 0


def _run_diagnostic_export(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    database.initialize()
    raw = database.diagnostic_raw_evidence(args.run_id)
    if raw is None:
        parser.error(f"unknown run: {args.run_id}")
    emit(raw)
    return 0


def _run_diagnostic_hpo_trial(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    database.initialize()
    query = getattr(database, "diagnostic_hpo_trial_details", None)
    detail = (
        query(args.study_id, args.trial_number) if query else None
    )
    if detail is None:
        parser.error(
            f"unknown HPO trial: {args.study_id}/{args.trial_number}"
        )
    emit(detail)
    return 0


def _run_hpo(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if args.limit < 1 or args.limit > 5000:
        parser.error("--limit must be between 1 and 5000")
    database.initialize()
    filters = {
        key: value for key, value in (
            ("lifecycle_state", args.state),
            ("strategy", args.strategy),
        ) if value
    }
    query = getattr(database, "hpo_studies", None)
    rows = query(filters=filters, limit=args.limit) if query else []
    if args.doctor:
        readiness = operator_status(database)["hpo"]
        if args.format == "json":
            emit(readiness.get("route_readiness", {}))
        else:
            print(render_hpo_readiness(readiness))
    elif args.format == "json":
        emit(rows)
    else:
        print(render_hpo_studies(rows))
    return 0


def _run_hpo_detail(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    database.initialize()
    detail = hpo_detail_snapshot(database, args.study_id)
    if detail is None:
        parser.error(f"unknown HPO study: {args.study_id}")
    if args.format == "json":
        emit(detail)
    else:
        print(render_hpo_detail(detail))
    return 0


def _run_hpo_route_plan(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    database.initialize()
    try:
        plan = HpoRoutePlanner(database).build(args.study_id)
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    if args.format == "json":
        emit(plan.to_dict())
    else:
        print(render_hpo_route_plan(plan))
    return 0


def _run_hpo_defaults(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    database.initialize()
    routes = default_hpo_routes(load_resource_policy(repo / ".ats-lab" / "config.toml"))
    eligible = database.hpo_studies_needing_default_routes()
    if args.study_id:
        eligible = [
            item for item in eligible
            if item.get("study_id") == args.study_id
        ]
        if not eligible:
            parser.error(
                "study is not a scheduled HPO study with empty routes: "
                f"{args.study_id}"
            )
    applied = []
    if args.apply:
        for item in eligible:
            applied.append(database.configure_default_hpo_routes(
                str(item["study_id"]), routes,
            ))
    payload = {
        "policy": routes,
        "eligible": eligible,
        "applied": [item.get("study_id") for item in applied],
        "next_action": (
            "ats-lab hpo --doctor" if applied
            else "ats-lab hpo-defaults --apply"
        ),
    }
    if args.format == "json":
        emit(payload)
    else:
        print("HPO DEFAULT ROUTES  disjoint historical bootstrap policy")
        for split, route_list in routes.items():
            route = route_list[0]
            print(
                f"{split:<8} {route['symbol']:<9} {route['timeframe']:<4} "
                f"{route['start_date']} -> {route['finish_date']}"
            )
        print(f"ELIGIBLE  {len(eligible)}")
        if args.apply:
            print(f"APPLIED   {len(applied)}")
        else:
            print("NEXT      ats-lab hpo-defaults --apply")
    return 0


def _run_timings(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if args.limit < 1 or args.limit > 5000:
        parser.error("--limit must be between 1 and 5000")
    database.initialize()
    query = getattr(database, "work_item_stage_timings", None)
    rows = query(
        work_item_id=args.job, limit=args.limit,
    ) if query else []
    if args.format == "json":
        emit(rows)
    else:
        print(render_stage_timings(rows))
    return 0


def _run_telemetry(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    if args.since_hours <= 0:
        parser.error("--since-hours must be positive")
    path = args.path or (repo / ".ats-lab" / "agent-transport.jsonl")
    result = TelemetryRollup(path).summarize(
        since_hours=args.since_hours,
    )
    if args.format == "json":
        emit(result)
    else:
        print(f"TELEMETRY  since={args.since_hours:g}h  path={path}")
        for item in result["task_types"]:
            input_stats = item["input_tokens"]
            output_stats = item["output_tokens"]
            cache_stats = item["cache_read_tokens"]
            print(
                f"{item['task_type']:<18} records={item['records']:<5} "
                f"input={input_stats['total']:.0f} "
                f"output={output_stats['total']:.0f} "
                f"cache={cache_stats['total']:.0f}"
            )
        for alarm in result["alarms"]:
            print(
                f"ALARM {alarm['code']} task={alarm['task_type']} "
                f"value={alarm['value']} detail={alarm['detail']}"
            )
    return 0


def _run_analyzer(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    query = getattr(database, "current_analyzer_status", None)
    state = query() if query else None
    if args.format == "json":
        emit(state)
    else:
        print(render_analyzer(state))
    return 0


def _run_requeue_hpo_analysis(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    database.initialize()
    try:
        emit(database.requeue_terminal_hpo_analysis(
            args.job_id,
            reason=args.reason,
            updated_by=args.operator,
        ))
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    return 0


def _run_requeue_hpo_execution(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    database.initialize()
    try:
        emit(database.requeue_hpo_execution(
            args.study_id,
            reason=args.reason,
            updated_by=args.operator,
        ))
    except (KeyError, ValueError) as error:
        parser.error(str(error))
    return 0


def _run_configure_hpo_validation_routes(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    database.initialize()
    route_path = (
        args.file if args.file.is_absolute() else repo / args.file
    )
    try:
        routes = json.loads(route_path.read_text())
        if not isinstance(routes, dict):
            raise ValueError("route file must contain an object by split")
        emit(database.configure_hpo_validation_routes(
            args.study_id, routes, updated_by=args.operator,
        ))
    except (KeyError, OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


def _run_hpo_import(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    database.initialize()
    source_path = args.file if args.file.is_absolute() else repo / args.file
    try:
        classifications = _read_hpo_classifications(
            repo, args.classifications,
        )
        result = import_optuna_study(
            database,
            source_path,
            study_name=args.study_name,
            target_study_id=args.study_id,
            classifications=classifications,
        )
    except (KeyError, OSError, ValueError, json.JSONDecodeError,
            sqlite3.Error) as error:
        parser.error(str(error))
    if args.format == "json":
        emit(result)
    else:
        print(
            f"HPO IMPORTED  study={result['study_id']} "
            f"trials={result['trials_imported']} "
            f"evidence={result['normalized_evidence_rows']} "
            f"job={result['analysis_job_id']} state={result['lifecycle_state']}"
        )
    return 0


def _run_hpo_import_jesse_session(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    database.initialize()
    source_path = args.file if args.file.is_absolute() else repo / args.file
    try:
        result = import_jesse_session_export(
            database,
            source_path,
            target_study_id=args.study_id,
            classifications=_read_hpo_classifications(
                repo, args.classifications,
            ),
        )
    except (KeyError, OSError, ValueError, json.JSONDecodeError,
            sqlite3.Error) as error:
        parser.error(str(error))
    if args.format == "json":
        emit(result)
    else:
        print(
            f"JESSE HPO IMPORTED  study={result['study_id']} "
            f"session={result['source_session_id']} "
            f"trials={result['trials_imported']} "
            f"evidence={result['normalized_evidence_rows']} "
            f"job={result['analysis_job_id']} state={result['lifecycle_state']}"
        )
    return 0


def _run_claim(context: CommandContext) -> int:
    database = context.database
    database.initialize()
    emit(database.claim_next(context.args.worker))
    return 0


def _run_inventory(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    result = build_inventory(repo)
    if args.markdown:
        output = args.markdown if args.markdown.is_absolute() else repo / args.markdown
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render_inventory(result))
    emit({category: len(items) for category, items in result.items()})
    return 0


def _run_enqueue(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    contract_path = args.file if args.file.is_absolute() else repo / args.file
    payload = load_json(contract_path)
    experiment = experiment_from_payload(payload["experiment"], str(contract_path.relative_to(repo)))
    work_item = work_item_from_payload(payload["work_item"])
    if work_item.experiment_id != experiment.id:
        raise ValueError("work_item.experiment_id must equal experiment.id")
    database.upsert_experiment(experiment)
    stored = database.upsert_work_item(work_item)
    emit({"experiment_id": experiment.id, "work_item_id": work_item.id, "state": stored["state"]})
    return 0


def _run_evaluate(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    contract_path = args.file if args.file.is_absolute() else repo / args.file
    evaluation = evaluation_from_payload(load_json(contract_path))
    database.add_evaluation(evaluation)
    emit({"experiment_id": evaluation.experiment_id, "verdict": evaluation.verdict.value})
    return 0


def _run_finish(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    emit(database.transition_work_item(args.work_item_id, WorkState.FINISHED, allowed_from=(WorkState.RUNNING,)))
    return 0


def _run_block(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    emit(database.transition_work_item(args.work_item_id, WorkState.BLOCKED,
         allowed_from=(WorkState.SCHEDULED, WorkState.READY, WorkState.RUNNING, WorkState.WAITING_RETRY),
         blocker_code=args.code, blocker_detail=args.detail))
    return 0


def _run_retry(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    emit(database.transition_work_item(args.work_item_id, WorkState.WAITING_RETRY,
         allowed_from=(WorkState.RUNNING, WorkState.BLOCKED), retry_after=args.after))
    return 0


def _run_reconcile(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    result = build_reconciliation(database, stale_after_hours=args.stale_after_hours)
    result["applied"] = apply_reconciliation(database, result) if args.apply else False
    emit(result)
    return 0


def _run_normalize_blockers(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    emit(normalize_unattempted_blockers(database, apply=args.apply))
    return 0


def _run_backfill_route_coverage(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    emit(backfill_aggregate_route_coverage(
        database, apply=args.apply,
        policy=load_resource_policy(repo / ".ats-lab" / "config.toml"),
    ))
    return 0


def _run_recover_partial_batch_retries(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    policy = load_resource_policy(repo / ".ats-lab" / "config.toml")
    emit(recover_partial_batch_retries(
        database, args.work_items, apply=args.apply,
        active_limit=policy.active_ready_limit,
    ))
    return 0


def _run_repair_data_routes(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    routes = []
    for raw_route in args.routes:
        route = json.loads(raw_route)
        if not isinstance(route, dict):
            raise ValueError("--route must decode to a JSON object")
        routes.append(route)
    if not args.apply:
        emit({
            "apply": False,
            "work_item_id": args.work_item_id,
            "data_routes": routes,
            "reason": args.reason,
        })
        return 0
    policy = load_resource_policy(context.repo / ".ats-lab" / "config.toml")
    emit(database.repair_work_item_data_routes(
        args.work_item_id, routes, reason=args.reason,
        active_limit=policy.active_ready_limit,
    ))
    return 0


def _run_recover_executor_infrastructure(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    policy = load_resource_policy(repo / ".ats-lab" / "config.toml")
    emit(recover_executor_infrastructure_failures(
        database, apply=args.apply, worker_id=args.worker,
        active_limit=policy.active_ready_limit,
    ))
    return 0


def _run_recover_orphaned_replacements(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    policy = load_resource_policy(repo / ".ats-lab" / "config.toml")
    emit(recover_orphaned_replacement_reservations(
        database, apply=args.apply,
        active_limit=policy.active_ready_limit,
    ))
    return 0


def _run_recover_zombie_sessions(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    database.initialize()
    config = load_direct_execution_config(repo / ".ats-lab" / "config.toml")
    client = McpClient(config.mcp_url, config.timeout_seconds)
    client.initialize()
    observations = {}
    for session_id in sorted(set(args.session_ids)):
        # Recovery requires unchanged_observations=2: two identical reads are
        # the minimum evidence that a session stopped executing.
        observations[session_id] = [
            DirectMcpDispatcher._session(client.call_tool(
                "get_backtest_session", {"session_id": session_id},
            )),
            DirectMcpDispatcher._session(client.call_tool(
                "get_backtest_session", {"session_id": session_id},
            )),
        ]
    policy = load_resource_policy(repo / ".ats-lab" / "config.toml")
    emit(recover_zombie_execution_sessions(
        database, observations, apply=args.apply,
        grace_seconds=config.zombie_grace_seconds,
        active_limit=policy.active_ready_limit,
    ))
    return 0


def _run_recovery_audit(context: CommandContext) -> int:
    repo = context.repo
    database = context.database
    database.initialize()
    config = load_direct_execution_config(repo / ".ats-lab" / "config.toml")
    session_ids = [
        row["session_id"] for row in database.rows(
            """SELECT DISTINCT d.session_id FROM direct_execution_sessions d
               JOIN work_items w ON w.id=d.work_item_id
               WHERE w.state IN ('waiting_retry','blocked')"""
        )
    ]
    observations = {}
    observation_degraded = False
    try:
        client = McpClient(config.mcp_url, config.timeout_seconds)
        client.initialize()
        for session_id in session_ids:
            observations[session_id] = [
                DirectMcpDispatcher._session(client.call_tool(
                    "get_backtest_session", {"session_id": session_id},
                )),
                DirectMcpDispatcher._session(client.call_tool(
                    "get_backtest_session", {"session_id": session_id},
                )),
            ]
    except Exception:
        observation_degraded = True
    result = classify_recovery_candidates(database, observations)
    emit({
        "session_observation_degraded": observation_degraded,
        "counts": {key: len(value) for key, value in result.items()},
        "categories": result,
    })
    return 0


def _run_sanitize(context: CommandContext) -> int:
    args = context.args
    database = context.database
    database.initialize()
    plan = build_sanitize_plan(database)
    plan["applied"] = apply_sanitize_plan(database, plan) if args.apply else False
    for item in plan["evaluate_finished"]:
        item.pop("metrics", None)
    emit(plan)
    return 0


def _run_synthesize(context: CommandContext) -> int:
    args = context.args
    repo = context.repo
    database = context.database
    contract_path = args.file if args.file.is_absolute() else repo / args.file
    request = synthesis_request_from_file(contract_path)
    try:
        source_path = str(contract_path.relative_to(repo))
    except ValueError:
        source_path = str(contract_path)
    emit(synthesize(database, request, source_path=source_path))
    return 0


def _run_worker(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    launcher_config = repo / ".ats-lab" / "config.toml"
    dispatch_command = args.dispatch_command
    if not dispatch_command:
        if launcher_config.is_file():
            dispatch_command = " ".join((
                shlex.quote(sys.executable), "-m", "ats_lab.agent_launcher",
                shlex.quote(str(launcher_config)),
            ))
        else:
            parser.error(
                "worker requires --dispatch-command, ATS_LAB_DISPATCH_COMMAND, "
                "or .ats-lab/config.toml"
            )
    if args.idle_sleep < 0 or args.retry_delay < 0:
        parser.error("worker sleep values must be non-negative")
    if args.max_items is not None and args.max_items < 1:
        parser.error("--max-items must be at least 1")
    database.initialize()
    try:
        results = Worker(
            database, CommandDispatcher(dispatch_command), args.worker,
            retry_delay_seconds=args.retry_delay,
            max_attempts=args.max_attempts,
            synthesize_when_idle=not args.no_idle_synthesis,
            resource_policy=load_resource_policy(launcher_config),
        ).run(
            continuous=args.continuous, idle_sleep=args.idle_sleep, max_items=args.max_items,
            on_result=emit_progress if args.continuous else None,
        )
    except KeyboardInterrupt:
        emit({"status": "stopped", "reason": "keyboard_interrupt"})
        return 130
    if not args.continuous:
        emit(results)
    return 0


def _run_supervisor(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    repo = context.repo
    database = context.database
    launcher_config = repo / ".ats-lab" / "config.toml"
    dispatch_command = args.dispatch_command
    if not dispatch_command:
        if launcher_config.is_file():
            dispatch_command = " ".join((
                shlex.quote(sys.executable), "-m", "ats_lab.agent_launcher",
                shlex.quote(str(launcher_config)),
            ))
        elif not args.plan:
            parser.error(
                "supervisor requires --dispatch-command, ATS_LAB_DISPATCH_COMMAND, "
                "or .ats-lab/config.toml"
            )
    if args.idle_sleep < 0 or args.retry_delay < 0:
        parser.error("supervisor sleep values must be non-negative")
    if args.max_rounds is not None and args.max_rounds < 1:
        parser.error("--max-rounds must be at least 1")
    database.initialize()
    policy = load_resource_policy(launcher_config)
    if args.plan:
        emit(BatchSupervisor(
            database, CommandDispatcher(dispatch_command or "true"), args.worker,
            resource_policy=policy,
        ).plan())
        return 0
    try:
        execution_config = load_direct_execution_config(launcher_config)
        stack_preflight = StackPreflight(
            dashboard_url=execution_config.dashboard_api_base_url,
            mcp_url=execution_config.mcp_url,
            postgres_container=os.environ.get(
                "ATS_LAB_JESSE_POSTGRES_CONTAINER", "postgres",
            ),
            postgres_user=os.environ.get(
                "ATS_LAB_JESSE_POSTGRES_USER", "jesse_user",
            ),
            postgres_database=os.environ.get(
                "ATS_LAB_JESSE_POSTGRES_DATABASE", "jesse_db",
            ),
            memory_health_url=os.environ.get(
                "ATS_LAB_MEMORY_HEALTH_URL",
                "http://127.0.0.1:18000/health",
            ),
        )
        fallback_dispatcher = CommandDispatcher(dispatch_command)
        dispatcher = DirectMcpDispatcher(
            database, execution_config,
            fallback=fallback_dispatcher,
            resource_policy=policy,
        )
        results = BatchSupervisor(
            database, dispatcher, args.worker,
            resource_policy=policy, retry_delay_seconds=args.retry_delay,
            max_attempts=args.max_attempts,
            preflight=stack_preflight.check,
            memory_adapter=_memory_adapter(),
        ).run(
            continuous=args.continuous, idle_sleep=args.idle_sleep,
            max_rounds=args.max_rounds,
            on_result=emit_progress if args.continuous else None,
        )
    except KeyboardInterrupt:
        emit({"status": "stopped", "reason": "keyboard_interrupt"})
        return 130
    if not args.continuous:
        emit(results)
    return 0


def _run_dashboard(context: CommandContext) -> int:
    args = context.args
    parser = context.parser
    database = context.database
    if not 0 <= args.port <= 65535:
        parser.error("dashboard port must be between 0 and 65535")
    database.initialize()
    serve_dashboard(database, args.host, args.port)
    return 0


REPO_DISCOVERY_COMMANDS = frozenset({
    "worker", "supervisor", "dashboard", "backend", "web", "status", "start", "monitor", "control",
    "console", "recover-claims", "resolve-blocker", "requeue-evaluation",
    "recover-orphaned-replacements",
    "repair-data-routes",
    "queue", "candidates", "evidence", "diagnostic-export",
    "diagnostic-hpo-trial",
    "hpo", "hpo-detail", "hpo-route-plan", "hpo-defaults", "timings", "telemetry", "analyzer",
    "requeue-hpo-analysis", "requeue-hpo-execution",
    "configure-hpo-validation-routes",
    "memory-status", "memory-sync", "memory", "memory-backfill",
    "home", "next", "doctor", "preflight", "recovery-audit", "tui", "loop",
})

CONTRACT_ERRORS = (OSError, ValueError, KeyError, sqlite3.Error)

COMMAND_HANDLERS: dict[str, Callable[[CommandContext], int]] = {
    "home": _run_home,
    "next": _run_next,
    "doctor": _run_doctor,
    "init": _run_init,
    "preflight": _run_preflight,
    "backend": _run_backend,
    "web": _run_web,
    "memory-status": _run_memory_status,
    "memory": _run_memory,
    "memory-sync": _run_memory_sync,
    "memory-backfill": _run_memory_backfill,
    "migrate-legacy": _run_migrate_legacy,
    "audit": _run_audit,
    "queue": _run_queue,
    "synthesis-status": _run_synthesis_status,
    "status": _run_status,
    "start": _run_start,
    "monitor": _run_monitor,
    "tui": _run_tui,
    "loop": _run_loop,
    "control": _run_control,
    "console": _run_console,
    "recover-claims": _run_recover_claims,
    "resolve-blocker": _run_resolve_blocker,
    "requeue-evaluation": _run_requeue_evaluation,
    "candidates": _run_candidates,
    "evidence": _run_evidence,
    "diagnostic-export": _run_diagnostic_export,
    "diagnostic-hpo-trial": _run_diagnostic_hpo_trial,
    "hpo": _run_hpo,
    "hpo-detail": _run_hpo_detail,
    "hpo-route-plan": _run_hpo_route_plan,
    "hpo-defaults": _run_hpo_defaults,
    "timings": _run_timings,
    "telemetry": _run_telemetry,
    "analyzer": _run_analyzer,
    "requeue-hpo-analysis": _run_requeue_hpo_analysis,
    "requeue-hpo-execution": _run_requeue_hpo_execution,
    "configure-hpo-validation-routes": _run_configure_hpo_validation_routes,
    "hpo-import": _run_hpo_import,
    "hpo-import-jesse-session": _run_hpo_import_jesse_session,
    "claim": _run_claim,
    "inventory": _run_inventory,
    "enqueue": _run_enqueue,
    "evaluate": _run_evaluate,
    "finish": _run_finish,
    "block": _run_block,
    "retry": _run_retry,
    "reconcile": _run_reconcile,
    "normalize-blockers": _run_normalize_blockers,
    "backfill-route-coverage": _run_backfill_route_coverage,
    "recover-partial-batch-retries": _run_recover_partial_batch_retries,
    "repair-data-routes": _run_repair_data_routes,
    "recover-executor-infrastructure": _run_recover_executor_infrastructure,
    "recover-orphaned-replacements": _run_recover_orphaned_replacements,
    "recover-zombie-sessions": _run_recover_zombie_sessions,
    "recovery-audit": _run_recovery_audit,
    "sanitize": _run_sanitize,
    "synthesize": _run_synthesize,
    "worker": _run_worker,
    "supervisor": _run_supervisor,
    "dashboard": _run_dashboard,
}


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        args.command = "home"
    if args.command == "help":
        print(ROOT_HELP, end="" if ROOT_HELP.endswith("\n") else "\n")
        return 0
    repo = args.repo.resolve()
    if args.command in REPO_DISCOVERY_COMMANDS and repo == Path.cwd().resolve():
        repo = discover_lab_repo(
            repo, fallback=Path(__file__).resolve().parents[2],
        )
    database_path = args.database if args.database.is_absolute() else repo / args.database
    database = WorkflowDatabase(database_path)
    context = CommandContext(
        parser=parser, args=args, repo=repo,
        database=database, database_path=database_path,
    )
    try:
        return COMMAND_HANDLERS[args.command](context)
    except CONTRACT_ERRORS as error:
        print(f"ats-lab: error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
