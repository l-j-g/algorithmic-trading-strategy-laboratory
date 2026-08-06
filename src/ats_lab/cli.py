"""Short idempotent CLI entry points for Agent/Memory orchestration."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .audit import build_audit, render_markdown
from .database import WorkflowDatabase
from .hpo_routes import HpoRoutePlanner, render_hpo_route_plan
from .direct_mcp_executor import (
    DirectMcpDispatcher,
    McpClient,
    load_direct_execution_config,
)
from .dashboard import serve as serve_dashboard
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


def main() -> int:
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
    memory_init.add_argument(
        "--format", choices=("table", "json"), default="table",
    )
    memory_status_nested = memory_sub.add_parser(
        "status", help="Show research-memory readiness."
    )
    memory_status_nested.add_argument(
        "--format", choices=("table", "json"), default="table",
    )
    memory_sync_nested = memory_sub.add_parser(
        "sync", help="Deliver currently queued research memory to Memory."
    )
    memory_sync_nested.add_argument("--dry-run", action="store_true")
    memory_sync_nested.add_argument("--limit", type=int, default=100)
    memory_sync_nested.add_argument(
        "--format", choices=("table", "json"), default="table",
    )
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
    claim = sub.add_parser("claim")
    claim.add_argument("--worker", default=os.environ.get("ATS_LAB_WORKER_ID", "ats-lab"))
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
    dashboard.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.command is None:
        args.command = "home"
    if args.command == "help":
        print(ROOT_HELP, end="" if ROOT_HELP.endswith("\n") else "\n")
        return 0
    repo = args.repo.resolve()
    if args.command in {
        "worker", "supervisor", "dashboard", "status", "monitor", "control",
        "console", "recover-claims", "resolve-blocker", "requeue-evaluation",
        "queue", "candidates", "evidence", "diagnostic-export",
        "diagnostic-hpo-trial",
        "hpo", "hpo-detail", "hpo-route-plan", "timings", "telemetry", "analyzer",
        "requeue-hpo-analysis", "configure-hpo-validation-routes",
        "memory-status", "memory-sync", "memory", "memory-backfill",
        "home", "next", "doctor", "preflight", "recovery-audit", "tui", "loop",
    } and repo == Path.cwd().resolve():
        repo = discover_lab_repo(
            repo, fallback=Path(__file__).resolve().parents[2],
        )
    database_path = args.database if args.database.is_absolute() else repo / args.database
    database = WorkflowDatabase(database_path)

    if args.command == "home":
        database.initialize()
        snapshot = monitor_snapshot(database)
        print(render_home(snapshot, memory_status(database)))
    elif args.command == "next":
        database.initialize()
        guidance = next_guidance(
            monitor_snapshot(database), memory_status(database),
        )
        if args.format == "json":
            emit(guidance)
        else:
            print(render_guidance(guidance))
    elif args.command == "doctor":
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
    elif args.command == "init":
        database.initialize()
        emit({"database": str(database_path), "status": "initialized"})
    elif args.command == "preflight":
        result = build_stack_preflight(repo).check()
        emit(result)
        return 0 if result["healthy"] else 2
    elif args.command == "memory-status":
        database.initialize()
        emit(memory_status(database))
    elif args.command == "memory":
        database.initialize()
        if args.memory_command == "status":
            result = memory_status(database)
            if args.format == "json":
                emit(result)
            else:
                print(render_memory_status(result))
        elif args.memory_command == "init":
            if args.batch_size < 1 or args.batch_size > 1000:
                parser.error("memory init --batch-size must be between 1 and 1000")
            if args.delivery_limit < 1 or args.delivery_limit > 100:
                parser.error("memory init --delivery-limit must be between 1 and 100")
            adapter = None if args.dry_run else MemoryResearchAdapter(
                MemoryProviderConfig(base_url=os.environ.get(
                    "ATS_LAB_MEMORY_URL", "http://127.0.0.1:18000",
                ))
            )

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
            if args.format == "json":
                emit(result)
            else:
                print(render_memory_init(result))
        elif args.memory_command == "sync":
            if args.limit < 1 or args.limit > 100:
                parser.error("memory sync --limit must be between 1 and 100")
            adapter = MemoryResearchAdapter(MemoryProviderConfig(
                base_url=os.environ.get(
                    "ATS_LAB_MEMORY_URL", "http://127.0.0.1:18000",
                )
            ))
            result = sync_memory_outbox(
                database, adapter, apply=not args.dry_run, limit=args.limit,
            )
            if args.format == "json":
                emit(result)
            else:
                print(render_memory_sync(result))
    elif args.command == "memory-sync":
        if args.limit < 1 or args.limit > 100:
            parser.error("memory-sync --limit must be between 1 and 100")
        database.initialize()
        adapter = MemoryResearchAdapter(MemoryProviderConfig(
            base_url=os.environ.get(
                "ATS_LAB_MEMORY_URL", "http://127.0.0.1:18000",
            )
        ))
        emit(sync_memory_outbox(
            database, adapter, apply=args.apply, limit=args.limit,
        ))
    elif args.command == "memory-backfill":
        if args.batch_size < 1 or args.batch_size > 1000:
            parser.error("memory-backfill --batch-size must be between 1 and 1000")
        database.initialize()
        emit(backfill_memory_outbox(
            database, apply=args.apply, batch_size=args.batch_size,
        ))
    elif args.command == "migrate-legacy":
        emit(LegacyImporter(repo, database).import_all())
    elif args.command == "audit":
        database.initialize()
        result = build_audit(database)
        if args.markdown:
            output = args.markdown if args.markdown.is_absolute() else repo / args.markdown
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_markdown(result))
            result["markdown"] = str(output)
        emit(result)
    elif args.command == "queue":
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
    elif args.command == "synthesis-status":
        database.initialize()
        emit(database.synthesis_status())
    elif args.command == "status":
        database.initialize()
        if args.format == "json":
            emit(operator_status(database))
        else:
            print(render_monitor(
                monitor_snapshot(database), color=sys.stdout.isatty() and not os.environ.get("NO_COLOR"),
            ))
    elif args.command == "monitor":
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
    elif args.command == "tui":
        if args.interval <= 0:
            parser.error("tui --interval must be positive")
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            parser.error("tui requires an interactive terminal; use ats-lab monitor")
        database.initialize()
        try:
            return run_tui(database, repo=repo, interval=args.interval)
        except KeyboardInterrupt:
            return 130
    elif args.command == "loop":
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
    elif args.command == "control":
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
    elif args.command == "console":
        if args.interval <= 0:
            parser.error("--interval must be positive")
        database.initialize()
        try:
            return run_console(database, interval=args.interval)
        except KeyboardInterrupt:
            print("\nconsole stopped")
            return 130
    elif args.command == "recover-claims":
        if args.stale_after_hours <= 0:
            parser.error("--stale-after-hours must be positive")
        database.initialize()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=args.stale_after_hours)
        ).isoformat().replace("+00:00", "Z")
        emit(database.recover_stale_unexecuted_claims(cutoff, apply=args.apply))
    elif args.command == "resolve-blocker":
        database.initialize()
        emit(database.resolve_blocked_work_item(
            args.work_item_id,
            resolution_code=args.code,
            detail=args.detail,
            evidence_ids=args.evidence,
        ))
    elif args.command == "requeue-evaluation":
        database.initialize()
        emit(database.requeue_finished_evaluation(
            args.work_item_id,
            worker_id=args.worker,
            reason=args.reason,
            batch_id=args.batch,
        ))
    elif args.command == "candidates":
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
    elif args.command == "evidence":
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
    elif args.command == "diagnostic-export":
        database.initialize()
        raw = database.diagnostic_raw_evidence(args.run_id)
        if raw is None:
            parser.error(f"unknown run: {args.run_id}")
        emit(raw)
    elif args.command == "diagnostic-hpo-trial":
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
    elif args.command == "hpo":
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
    elif args.command == "hpo-detail":
        database.initialize()
        detail = hpo_detail_snapshot(database, args.study_id)
        if detail is None:
            parser.error(f"unknown HPO study: {args.study_id}")
        if args.format == "json":
            emit(detail)
        else:
            print(render_hpo_detail(detail))
    elif args.command == "hpo-route-plan":
        database.initialize()
        try:
            plan = HpoRoutePlanner(database).build(args.study_id)
        except (KeyError, ValueError) as error:
            parser.error(str(error))
        if args.format == "json":
            emit(plan.to_dict())
        else:
            print(render_hpo_route_plan(plan))
    elif args.command == "timings":
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
    elif args.command == "telemetry":
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
    elif args.command == "analyzer":
        database.initialize()
        query = getattr(database, "current_analyzer_status", None)
        state = query() if query else None
        if args.format == "json":
            emit(state)
        else:
            print(render_analyzer(state))
    elif args.command == "requeue-hpo-analysis":
        database.initialize()
        try:
            emit(database.requeue_terminal_hpo_analysis(
                args.job_id,
                reason=args.reason,
                updated_by=args.operator,
            ))
        except (KeyError, ValueError) as error:
            parser.error(str(error))
    elif args.command == "configure-hpo-validation-routes":
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
    elif args.command == "claim":
        database.initialize()
        emit(database.claim_next(args.worker))
    elif args.command == "inventory":
        result = build_inventory(repo)
        if args.markdown:
            output = args.markdown if args.markdown.is_absolute() else repo / args.markdown
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(render_inventory(result))
        emit({category: len(items) for category, items in result.items()})
    elif args.command == "enqueue":
        database.initialize()
        contract_path = args.file if args.file.is_absolute() else repo / args.file
        payload = load_json(contract_path)
        experiment = experiment_from_payload(payload["experiment"], str(contract_path.relative_to(repo)))
        work_item = work_item_from_payload(payload["work_item"])
        if work_item.experiment_id != experiment.id:
            raise ValueError("work_item.experiment_id must equal experiment.id")
        database.upsert_experiment(experiment)
        database.upsert_work_item(work_item)
        emit({"experiment_id": experiment.id, "work_item_id": work_item.id, "state": work_item.state.value})
    elif args.command == "evaluate":
        database.initialize()
        contract_path = args.file if args.file.is_absolute() else repo / args.file
        evaluation = evaluation_from_payload(load_json(contract_path))
        database.add_evaluation(evaluation)
        emit({"experiment_id": evaluation.experiment_id, "verdict": evaluation.verdict.value})
    elif args.command == "finish":
        database.initialize()
        emit(database.transition_work_item(args.work_item_id, WorkState.FINISHED, allowed_from=(WorkState.RUNNING,)))
    elif args.command == "block":
        database.initialize()
        emit(database.transition_work_item(args.work_item_id, WorkState.BLOCKED,
             allowed_from=(WorkState.SCHEDULED, WorkState.READY, WorkState.RUNNING, WorkState.WAITING_RETRY),
             blocker_code=args.code, blocker_detail=args.detail))
    elif args.command == "retry":
        database.initialize()
        emit(database.transition_work_item(args.work_item_id, WorkState.WAITING_RETRY,
             allowed_from=(WorkState.RUNNING, WorkState.BLOCKED), retry_after=args.after))
    elif args.command == "reconcile":
        database.initialize()
        result = build_reconciliation(database, stale_after_hours=args.stale_after_hours)
        result["applied"] = apply_reconciliation(database, result) if args.apply else False
        emit(result)
    elif args.command == "normalize-blockers":
        database.initialize()
        emit(normalize_unattempted_blockers(database, apply=args.apply))
    elif args.command == "backfill-route-coverage":
        database.initialize()
        emit(backfill_aggregate_route_coverage(
            database, apply=args.apply,
            policy=load_resource_policy(repo / ".ats-lab" / "config.toml"),
        ))
    elif args.command == "recover-partial-batch-retries":
        database.initialize()
        policy = load_resource_policy(repo / ".ats-lab" / "config.toml")
        emit(recover_partial_batch_retries(
            database, args.work_items, apply=args.apply,
            active_limit=policy.active_ready_limit,
        ))
    elif args.command == "recover-executor-infrastructure":
        database.initialize()
        policy = load_resource_policy(repo / ".ats-lab" / "config.toml")
        emit(recover_executor_infrastructure_failures(
            database, apply=args.apply, worker_id=args.worker,
            active_limit=policy.active_ready_limit,
        ))
    elif args.command == "recover-zombie-sessions":
        database.initialize()
        config = load_direct_execution_config(repo / ".ats-lab" / "config.toml")
        client = McpClient(config.mcp_url, config.timeout_seconds)
        client.initialize()
        observations = {}
        for session_id in sorted(set(args.session_ids)):
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
    elif args.command == "recovery-audit":
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
    elif args.command == "sanitize":
        database.initialize()
        plan = build_sanitize_plan(database)
        plan["applied"] = apply_sanitize_plan(database, plan) if args.apply else False
        for item in plan["evaluate_finished"]:
            item.pop("metrics", None)
        emit(plan)
    elif args.command == "synthesize":
        contract_path = args.file if args.file.is_absolute() else repo / args.file
        request = synthesis_request_from_file(contract_path)
        try:
            source_path = str(contract_path.relative_to(repo))
        except ValueError:
            source_path = str(contract_path)
        emit(synthesize(database, request, source_path=source_path))
    elif args.command == "worker":
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
    elif args.command == "supervisor":
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
            )
            results = BatchSupervisor(
                database, dispatcher, args.worker,
                resource_policy=policy, retry_delay_seconds=args.retry_delay,
                max_attempts=args.max_attempts,
                preflight=stack_preflight.check,
                memory_adapter=MemoryResearchAdapter(MemoryProviderConfig(
                    base_url=os.environ.get(
                        "ATS_LAB_MEMORY_URL", "http://127.0.0.1:18000",
                    )
                )),
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
    elif args.command == "dashboard":
        if not 0 <= args.port <= 65535:
            parser.error("dashboard port must be between 0 and 65535")
        database.initialize()
        serve_dashboard(database, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
