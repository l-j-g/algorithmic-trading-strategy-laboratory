"""Short idempotent CLI entry points for Agent/Memory orchestration."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path

from .audit import build_audit, render_markdown
from .database import WorkflowDatabase
from .dashboard import serve as serve_dashboard
from .inventory import build_inventory, render_markdown as render_inventory
from .legacy_import import LegacyImporter
from .contracts import evaluation_from_payload, experiment_from_payload, load_json, work_item_from_payload
from .models import WorkState
from .reconcile import apply_reconciliation, build_reconciliation, normalize_unattempted_blockers
from .resources import load_resource_policy
from .sanitize import apply_sanitize_plan, build_sanitize_plan
from .synthesis import synthesis_request_from_file, synthesize
from .worker import CommandDispatcher, Worker


def emit(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def discover_lab_repo(start: Path) -> Path:
    """Return the nearest parent containing ATS Lab configuration."""
    resolved = start.resolve()
    return next(
        (
            candidate
            for candidate in (resolved, *resolved.parents)
            if (candidate / ".ats-lab" / "config.toml").is_file()
        ),
        resolved,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="ats-lab", description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path, default=Path(".ats-lab/laboratory.sqlite3"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("migrate-legacy")
    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--markdown", type=Path)
    queue_parser = sub.add_parser("queue")
    queue_parser.add_argument("--state")
    sub.add_parser("synthesis-status")
    candidates = sub.add_parser("candidates")
    candidates.add_argument("--verdict")
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
    sanitize = sub.add_parser("sanitize", help="Evaluate terminal evidence and delete dead active queue items.")
    sanitize.add_argument("--apply", action="store_true")
    synthesis = sub.add_parser("synthesize", help="Create gated jobs from a typed research idea.")
    synthesis.add_argument("--file", type=Path, required=True)
    worker = sub.add_parser("worker", help="Claim and dispatch ready work.")
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
    dashboard = sub.add_parser("dashboard", help="Serve the local read-only operator dashboard.")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    repo = args.repo.resolve()
    if args.command in {"worker", "dashboard"} and repo == Path.cwd().resolve():
        repo = discover_lab_repo(repo)
    database_path = args.database if args.database.is_absolute() else repo / args.database
    database = WorkflowDatabase(database_path)

    if args.command == "init":
        database.initialize()
        emit({"database": str(database_path), "status": "initialized"})
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
        emit(database.rows(query, parameters))
    elif args.command == "synthesis-status":
        database.initialize()
        emit(database.synthesis_status())
    elif args.command == "candidates":
        database.initialize()
        query = "SELECT * FROM candidate_summary"
        parameters = ()
        if args.verdict:
            query += " WHERE verdict = ?"
            parameters = (args.verdict.replace("-", "_"),)
        query += " ORDER BY CASE verdict WHEN 'paper_trade_candidate' THEN 0 WHEN 'hpo_candidate' THEN 1 ELSE 2 END, evaluated_at DESC"
        emit(database.rows(query, parameters))
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
                on_result=emit if args.continuous else None,
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
