"""Short idempotent CLI entry points for Agent/Memory orchestration."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .audit import build_audit, render_markdown
from .database import WorkflowDatabase
from .inventory import build_inventory, render_markdown as render_inventory
from .legacy_import import LegacyImporter
from .contracts import evaluation_from_payload, experiment_from_payload, load_json, work_item_from_payload
from .models import WorkState


def emit(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


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
    args = parser.parse_args()
    repo = args.repo.resolve()
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
