"""Compact operator status and next-action guidance."""
from __future__ import annotations

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
    latest_event = database.rows("SELECT MAX(occurred_at) AS at FROM events")[0]["at"]
    synthesis = database.synthesis_status()
    hpo = hpo_lifecycle_snapshot(database)
    ready = int(states.get("ready", 0))
    running = int(states.get("running", 0))
    scheduled = int(states.get("scheduled", 0))
    if stale_claims:
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
    return {
        "healthy": not bool(stale_claims),
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
