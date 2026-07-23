"""Classify imported queue state and safely reconcile stale legacy records."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import WorkflowDatabase
from .models import WorkState, utc_now


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def build_reconciliation(database: WorkflowDatabase, *, stale_after_hours: float = 24.0,
                         now: datetime | None = None) -> dict[str, Any]:
    """Return deterministic classifications without changing queue state."""
    if stale_after_hours < 0:
        raise ValueError("stale_after_hours must be non-negative")
    current = now or datetime.now(timezone.utc)
    cutoff = current - timedelta(hours=stale_after_hours)
    rows = database.rows(
        """SELECT w.*, EXISTS(SELECT 1 FROM evaluations ev WHERE ev.experiment_id=w.experiment_id) AS has_evaluation,
                  EXISTS(SELECT 1 FROM runs r WHERE r.experiment_id=w.experiment_id
                         AND r.status IN ('finished','stopped','terminated')) AS has_terminal_run
           FROM work_items w WHERE w.state IN ('scheduled','ready','running','waiting_retry','blocked')
           ORDER BY w.priority, w.created_at, w.id""")
    result: dict[str, Any] = {"generated_at": current.isoformat().replace("+00:00", "Z"),
        "stale_after_hours": stale_after_hours, "stale_running": [], "actionable": [], "historical_blockers": []}
    for row in rows:
        item = {"id": row["id"], "experiment_id": row["experiment_id"], "state": row["state"]}
        if row["state"] == WorkState.RUNNING.value:
            activity = _parse_timestamp(row["claimed_at"]) or _parse_timestamp(row["updated_at"])
            if row["claimed_at"] is None or activity is None or activity <= cutoff:
                item["last_activity_at"] = activity.isoformat().replace("+00:00", "Z") if activity else None
                item["reason"] = "missing_claim" if row["claimed_at"] is None else "claim_expired"
                result["stale_running"].append(item)
                continue
        historical = (row["state"] == WorkState.BLOCKED.value and row["blocker_code"] == "legacy_blocked"
                      and bool(row["has_evaluation"] or row["has_terminal_run"]))
        if historical:
            item["reason"] = "legacy blocker has evaluation or terminal run evidence"
            result["historical_blockers"].append(item)
        else:
            result["actionable"].append(item)
    result["counts"] = {key: len(result[key]) for key in ("stale_running", "actionable", "historical_blockers")}
    return result


def apply_reconciliation(database: WorkflowDatabase, result: dict[str, Any]) -> dict[str, Any]:
    """Apply only conservative transitions described by a reconciliation report."""
    changed = {"stale_running_blocked": [], "historical_blockers_archived": []}
    for item in result["stale_running"]:
        database.transition_work_item(item["id"], WorkState.BLOCKED, allowed_from=(WorkState.RUNNING,),
            blocker_code="stale_worker_claim", blocker_detail=f"Worker claim stale during reconciliation at {utc_now()}")
        changed["stale_running_blocked"].append(item["id"])
    for item in result["historical_blockers"]:
        database.transition_work_item(item["id"], WorkState.ARCHIVED, allowed_from=(WorkState.BLOCKED,),
            blocker_code="legacy_history", blocker_detail="Archived legacy blocker with persisted outcome evidence")
        changed["historical_blockers_archived"].append(item["id"])
    return changed


def normalize_unattempted_blockers(database: WorkflowDatabase, *, apply: bool = False) -> dict[str, Any]:
    """Move never-attempted legacy blockers back to scheduled backlog.

    Legacy blocker text remains in specification_json as readiness metadata.
    Runtime blockers are reserved for work that was actually attempted.
    """
    rows = database.rows(
        """SELECT id, blocker_code, blocker_detail, specification_json
           FROM work_items
           WHERE state='blocked' AND attempts=0
           ORDER BY priority, created_at, id"""
    )
    result: dict[str, Any] = {"eligible": len(rows), "work_item_ids": [row["id"] for row in rows], "applied": []}
    if not apply:
        return result
    now = utc_now()
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            try:
                specification = json.loads(row["specification_json"] or "{}")
            except json.JSONDecodeError:
                specification = {"legacy_specification": row["specification_json"]}
            specification["readiness"] = {
                "status": "requirements_pending",
                "source": "legacy_blocker",
                "code": row["blocker_code"],
                "detail": row["blocker_detail"],
            }
            updated = connection.execute(
                """UPDATE work_items
                   SET state='scheduled', blocker_code=NULL, blocker_detail=NULL,
                       specification_json=?, claimed_by=NULL, claimed_at=NULL, updated_at=?
                   WHERE id=? AND state='blocked' AND attempts=0""",
                (json.dumps(specification, sort_keys=True), now, row["id"]),
            )
            if not updated.rowcount:
                continue
            connection.execute(
                """INSERT INTO events(aggregate_type, aggregate_id, event_type, payload_json, occurred_at)
                   VALUES ('work_item', ?, 'legacy_blocker_normalized', ?, ?)""",
                (row["id"], json.dumps({"from": "blocked", "to": "scheduled", "readiness_preserved": True}), now),
            )
            result["applied"].append(row["id"])
    return result
