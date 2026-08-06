"""Compact operator status and next-action guidance."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .database import WorkflowDatabase


HPO_LIFECYCLE_STATES = (
    "hpo_candidate",
    "hpo_scheduled",
    "hpo_running",
    "hpo_analysis",
    "validation",
    "paper_trade_candidate",
    "revise",
    "reject",
)


def _route_count(specification: dict[str, Any], split: str) -> int:
    """Count configured routes without exposing route values."""
    if split == "hpo":
        routes = specification.get("routes")
    else:
        configured = specification.get("validation_routes")
        routes = configured.get(split) if isinstance(configured, dict) else None
    return len(routes) if isinstance(routes, list) else 0


def _hpo_route_readiness(
    database: WorkflowDatabase, studies: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project route readiness and queued validation work for operators.

    This is deliberately read-only. Route contents stay out of status output;
    operators get counts, missing split names, and one next action instead.
    """
    rows = database.rows(
        """SELECT s.id AS study_id,s.lifecycle_state,
                  e.specification_json AS experiment_json,
                  w.state AS hpo_work_state,w.blocker_code AS hpo_blocker
           FROM hpo_studies s
           JOIN experiments e ON e.id=s.hpo_experiment_id
           LEFT JOIN work_items w ON w.id=s.hpo_work_item_id
           ORDER BY s.updated_at DESC,s.id"""
    )
    by_id = {str(row["study_id"]): row for row in rows}
    entries: list[dict[str, Any]] = []
    missing_counts = {split: 0 for split in ("hpo", "oos", "rolling")}
    validation_counts = {"total": 0, "ready": 0, "pending": 0, "running": 0,
                         "finished": 0, "blocked": 0}
    for study in studies:
        study_id = str(study.get("study_id") or "")
        row = by_id.get(study_id)
        if row is None:
            continue
        try:
            specification = json.loads(row.get("experiment_json") or "{}")
        except (TypeError, ValueError):
            specification = {}
        if not isinstance(specification, dict):
            specification = {}
        routes = {
            split: _route_count(specification, split)
            for split in ("hpo", "oos", "rolling")
        }
        validation_jobs = database.rows(
            """SELECT v.evidence_split,w.state,w.blocker_code,
                      json_extract(w.specification_json,'$.readiness.status')
                      AS readiness_status
               FROM hpo_validation_jobs v
               JOIN work_items w ON w.id=v.work_item_id
               WHERE v.study_id=? ORDER BY v.evidence_split,v.id""",
            (study_id,),
        )
        study_pending_jobs = 0
        for job in validation_jobs:
            validation_counts["total"] += 1
            state = str(job.get("state") or "").lower()
            if state in validation_counts:
                validation_counts[state] += 1
            if state in {"ready", "scheduled"} and job.get("readiness_status") != "requirements_pending":
                validation_counts["ready"] += 1
            elif job.get("readiness_status") == "requirements_pending":
                validation_counts["pending"] += 1
                study_pending_jobs += 1
        missing: list[str] = []
        if row.get("hpo_work_state") in {"scheduled", "ready", "running"} and not routes["hpo"]:
            missing.append("hpo")
        expected_validation = bool(validation_jobs) or study.get("lifecycle_state") == "validation"
        if expected_validation:
            for split in ("oos", "rolling"):
                if not routes[split]:
                    missing.append(split)
        for split in missing:
            missing_counts[split] += 1
        if missing:
            next_action = "configure_hpo_validation_routes"
        elif study_pending_jobs:
            next_action = "configure_hpo_validation_routes"
        elif row.get("hpo_work_state") in {"scheduled", "ready", "running"}:
            next_action = "monitor_hpo_execution"
        else:
            next_action = str(study.get("next_action") or "review_hpo_detail")
        entries.append({
            "study_id": study_id,
            "strategy": study.get("strategy"),
            "lifecycle_state": study.get("lifecycle_state"),
            "hpo_work_state": row.get("hpo_work_state"),
            "hpo_blocker": row.get("hpo_blocker"),
            "routes": routes,
            "missing": missing,
            "validation_jobs": len(validation_jobs),
            "next_action": next_action,
        })
    return {
        "studies": entries,
        "missing_route_studies": sum(1 for item in entries if item["missing"]),
        "missing_routes": missing_counts,
        "validation_jobs": validation_counts,
        "next_action": (
            "configure_hpo_validation_routes"
            if any(item["missing"] for item in entries)
            else "monitor_hpo_execution"
            if any(item["hpo_work_state"] in {"scheduled", "ready", "running"} for item in entries)
            else "review_hpo_detail"
        ),
    }


def hpo_lifecycle_snapshot(database: WorkflowDatabase) -> dict[str, Any]:
    """Shared HPO lifecycle projection for dashboard and terminal consumers."""
    query = getattr(database, "hpo_studies", None)
    studies = query(limit=5000) if query is not None else []
    counts = {state: 0 for state in HPO_LIFECYCLE_STATES}
    for study in studies:
        state = study.get("lifecycle_state")
        if state in counts:
            counts[state] += 1
    analyzer_query = getattr(database, "current_analyzer_status", None)
    analyzer = analyzer_query() if analyzer_query is not None else None
    timing_query = getattr(database, "work_item_stage_timings", None)
    timings = timing_query(limit=20) if timing_query is not None else []
    route_readiness = _hpo_route_readiness(database, studies)
    return {
        "counts": counts,
        "total": len(studies),
        "active": sum(
            counts[state]
            for state in (
                "hpo_scheduled", "hpo_running", "hpo_analysis", "validation",
            )
        ),
        "analyzer": analyzer,
        "recent_timings": timings,
        "route_readiness": route_readiness,
    }


def hpo_detail_snapshot(
    database: WorkflowDatabase, study_id: str,
) -> dict[str, Any] | None:
    """Read one study, including synthetic unscheduled candidate records."""
    query = getattr(database, "hpo_study_detail", None)
    detail = query(study_id) if query is not None else None
    if detail is not None or not study_id.startswith("candidate:"):
        return detail
    studies_query = getattr(database, "hpo_studies", None)
    if studies_query is None:
        return None
    study = next(
        (
            row for row in studies_query(limit=5000)
            if row.get("study_id") == study_id
        ),
        None,
    )
    if study is None:
        return None
    return {
        **study,
        "selected_trials": [],
        "proposed_defaults": [],
        "narrowed_ranges": [],
        "validations": [],
        "analysis_job": None,
        "timings": [],
    }


def operator_status(
    database: WorkflowDatabase, claim_timeout_seconds: int = 7200,
) -> dict[str, Any]:
    states = {
        row["state"]: row["count"]
        for row in database.rows(
            "SELECT state,COUNT(*) AS count FROM work_items GROUP BY state ORDER BY state"
        )
    }
    awaiting = database.rows(
        """SELECT COUNT(*) AS count FROM work_items
           WHERE state='running' AND blocker_code='awaiting_batch_evaluation'"""
    )[0]["count"]
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=claim_timeout_seconds)).isoformat().replace(
        "+00:00", "Z"
    )
    claims = database.rows(
        """SELECT COUNT(*) AS count,MIN(claimed_at) AS claimed_at FROM work_items
           WHERE state='running'
             AND COALESCE(blocker_code,'')!='awaiting_batch_evaluation'"""
    )[0]
    running_claims = int(claims["count"])
    stale = database.rows(
        """SELECT COUNT(*) AS count,MIN(claimed_at) AS claimed_at FROM work_items
           WHERE state='running'
             AND COALESCE(blocker_code,'')!='awaiting_batch_evaluation'
             AND (claimed_at IS NULL OR claimed_at<?)""",
        (cutoff,),
    )[0]
    stale_claims = int(stale["count"])
    retry_rows = database.rows(
        """SELECT retry_after FROM work_items
           WHERE state='waiting_retry' AND retry_after IS NOT NULL"""
    )
    invalid_retry_schedules = sum(
        1 for row in retry_rows
        if str(row["retry_after"]).strip().replace(".", "", 1).isdigit()
    )
    latest_event = database.rows("SELECT MAX(occurred_at) AS at FROM events")[0]["at"]
    synthesis = database.synthesis_status()
    hpo = hpo_lifecycle_snapshot(database)
    ready = int(states.get("ready", 0))
    running = int(states.get("running", 0))
    scheduled = int(states.get("scheduled", 0))
    if invalid_retry_schedules:
        next_action = "repair_retry_schedules"
    elif stale_claims:
        next_action = "recover_or_inspect_running_claim"
    elif awaiting:
        next_action = "resume_batch_analysis"
    elif running_claims:
        next_action = "monitor_running_batch"
    elif ready:
        next_action = "execute_batch"
    elif scheduled:
        next_action = "promote_or_resolve_dependencies"
    elif synthesis["remaining_chains"] <= 5:
        next_action = "synthesize_cohort"
    else:
        next_action = "idle"
    progress_state = (
        "stalled" if stale_claims or invalid_retry_schedules
        else "running" if running_claims
        else "ready" if ready
        else "waiting" if scheduled or states.get("waiting_retry", 0)
        else "idle"
    )
    return {
        "healthy": not bool(stale_claims or invalid_retry_schedules),
        "progress_state": progress_state,
        "invalid_retry_schedules": invalid_retry_schedules,
        "checked_at": now.isoformat().replace("+00:00", "Z"),
        "database": str(database.path),
        "work_states": states,
        "awaiting_batch_evaluation": int(awaiting),
        "oldest_running_claim": claims["claimed_at"],
        "oldest_unresolved_claim": stale["claimed_at"],
        "running_execution_claims": running_claims,
        "unresolved_execution_claims": stale_claims,
        "latest_event": latest_event,
        "synthesis": synthesis,
        "hpo": hpo,
        "next_action": next_action,
        "active": ready + running + scheduled + int(states.get("waiting_retry", 0)),
        "blocked": int(states.get("blocked", 0)),
    }
