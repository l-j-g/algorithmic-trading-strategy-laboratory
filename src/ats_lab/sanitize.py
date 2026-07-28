"""Deterministic queue and legacy-history sanitation."""
from __future__ import annotations

import json
from typing import Any

from .database import WorkflowDatabase
from .models import Evaluation, Verdict, utc_now

ACTIVE_STATES = ("scheduled", "ready", "running", "waiting_retry", "blocked")


def _verdict(metrics: dict[str, Any]) -> Verdict:
    explicit = str(metrics.get("verdict", "")).replace("-", "_")
    if explicit in {item.value for item in Verdict}:
        return Verdict(explicit)
    p_value = metrics.get("p_value")
    if isinstance(p_value, (int, float)):
        return Verdict.PASS if p_value < 0.05 else (
            Verdict.INCONCLUSIVE if p_value <= 0.10 else Verdict.REJECT
        )
    routes = metrics.get("route_results")
    if isinstance(routes, list) and routes:
        passed = all(
            float(route.get("expectancy", route.get("net_profit", 0))) > 0
            and float(route.get("max_drawdown", 0)) > -30
            for route in routes
        )
        return Verdict.PASS if passed else Verdict.REJECT
    expectancy = float(metrics.get("expectancy", 0) or 0)
    net = float(metrics.get("net_profit", metrics.get("net_profit_percentage", 0)) or 0)
    drawdown = float(metrics.get("max_drawdown", 0) or 0)
    trades = int(metrics.get("total_trades", metrics.get("total", 0)) or 0)
    return Verdict.PASS if expectancy > 0 and net > 0 and drawdown > -30 and trades >= 30 else Verdict.REJECT


def build_sanitize_plan(database: WorkflowDatabase) -> dict[str, Any]:
    deletable = database.rows(
        """SELECT w.id,w.experiment_id,w.blocker_code,w.blocker_detail
           FROM work_items w
           WHERE (
               (w.state='blocked' AND w.attempts=0)
               OR w.state='archived'
           )
             AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.experiment_id=w.experiment_id)
             AND NOT EXISTS (SELECT 1 FROM evaluations e WHERE e.experiment_id=w.experiment_id)
           ORDER BY w.priority,w.created_at,w.id"""
    )
    missing = database.rows(
        """SELECT w.id,w.experiment_id,r.metrics_json,r.status
           FROM work_items w
           JOIN runs r ON r.id=(
               SELECT r2.id FROM runs r2 WHERE r2.experiment_id=w.experiment_id
               ORDER BY COALESCE(r2.finished_at,r2.started_at,'') DESC,r2.id DESC LIMIT 1
           )
           WHERE w.state='finished'
             AND NOT EXISTS (SELECT 1 FROM evaluations e WHERE e.experiment_id=w.experiment_id)
           ORDER BY w.id"""
    )
    evaluations = []
    for row in missing:
        metrics = json.loads(row["metrics_json"] or "{}")
        verdict = _verdict(metrics) if row["status"] == "finished" else Verdict.INFRASTRUCTURE_FAILURE
        evaluations.append({
            "work_item_id": row["id"],
            "experiment_id": row["experiment_id"],
            "verdict": verdict.value,
            "metrics": metrics,
        })
    return {
        "delete_work_items": deletable,
        "evaluate_finished": evaluations,
        "counts": {"delete": len(deletable), "evaluate": len(evaluations)},
    }


def apply_sanitize_plan(database: WorkflowDatabase, plan: dict[str, Any]) -> dict[str, Any]:
    evaluated: list[str] = []
    for item in plan["evaluate_finished"]:
        metrics = item["metrics"]
        summary = (
            f"Sanitized persisted terminal evidence; deterministic verdict={item['verdict']}. "
            "No new run was performed."
        )
        compact = json.dumps(metrics, separators=(",", ":"), sort_keys=True)
        database.add_evaluation(Evaluation(
            experiment_id=item["experiment_id"],
            verdict=Verdict(item["verdict"]),
            summary=summary,
            metrics_summary=compact[:4000],
            next_step="Terminal history sanitized; create a new explicit experiment for further work.",
            evaluator="ats-lab-sanitizer",
            evaluated_at=utc_now(),
        ))
        evaluated.append(item["work_item_id"])

    deleted = [item["id"] for item in plan["delete_work_items"]]
    if deleted:
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in plan["delete_work_items"]:
                connection.execute(
                    "DELETE FROM artifacts WHERE experiment_id=?", (item["experiment_id"],)
                )
                connection.execute(
                    "DELETE FROM events WHERE aggregate_type='work_item' AND aggregate_id=?",
                    (item["id"],),
                )
                connection.execute("DELETE FROM work_items WHERE id=?", (item["id"],))
                connection.execute(
                    """INSERT INTO events(aggregate_type,aggregate_id,event_type,payload_json,occurred_at)
                       VALUES ('sanitizer',?,'dead_queue_item_deleted',?,?)""",
                    (
                        item["id"],
                        json.dumps({
                            "experiment_id": item["experiment_id"],
                            "blocker_code": item["blocker_code"],
                            "blocker_detail": item["blocker_detail"],
                        }, sort_keys=True),
                        utc_now(),
                    ),
                )
    return {"deleted": deleted, "evaluated": evaluated}
