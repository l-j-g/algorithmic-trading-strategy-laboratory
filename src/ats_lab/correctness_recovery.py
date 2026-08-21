"""Deterministic repairs for persisted route coverage and batch retry accounting."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .database import WorkflowDatabase
from .direct_mcp_executor import DirectMcpDispatcher, classify_jesse_session
from .gates import evaluate_gates
from .models import Evaluation, utc_now
from .resources import ResourcePolicy


_PARTIAL_BATCH_MARKERS = (
    "batch_execution_failed",
    "significance-test session remains running",
    "seven work items finished",
)

_INFRASTRUCTURE_MARKERS = (
    "transport", "provider", "timeout", "mcp_error", "preflight",
    "execution_deferred", "zombie", "memory", "executor_failed",
)


def classify_recovery_candidates(
    database: WorkflowDatabase,
    observations: Mapping[str, list[dict]] | None = None,
) -> dict[str, list[dict]]:
    """Evidence-audit retry/blocker populations without changing workflow state."""
    observations = observations or {}
    categories: dict[str, list[dict]] = {
        "valid_strategy_or_harness_failures": [],
        "infrastructure_transport_failures": [],
        "stopped_sessions_that_executed": [],
        "stopped_or_nonstarted_without_evidence": [],
        "dependency_only_blockers": [],
        "irreducible_blockers": [],
    }
    rows = database.rows(
        """SELECT w.id,w.state,w.attempts,w.blocker_code,w.blocker_detail,
                  w.dependencies_json,d.session_id,d.state AS checkpoint_state,
                  d.error_text
           FROM work_items w
           LEFT JOIN direct_execution_sessions d ON d.work_item_id=w.id
           WHERE w.state IN ('waiting_retry','blocked') ORDER BY w.state,w.id"""
    )
    for row in rows:
        item = {
            "work_item_id": row["id"], "state": row["state"],
            "attempts": row["attempts"], "blocker_code": row["blocker_code"],
        }
        dependencies = json.loads(row["dependencies_json"] or "[]")
        unresolved = []
        if dependencies:
            placeholders = ",".join("?" for _ in dependencies)
            states = {
                dependency["id"]: dependency["state"]
                for dependency in database.rows(
                    f"SELECT id,state FROM work_items WHERE id IN ({placeholders})",
                    tuple(dependencies),
                )
            }
            unresolved = [
                dependency for dependency in dependencies
                if states.get(dependency) not in {"finished", "archived"}
            ]
        if unresolved:
            item["reason"] = "unresolved_dependencies"
            categories["dependency_only_blockers"].append(item)
            continue
        session_id = row["session_id"]
        samples = observations.get(session_id, []) if session_id else []
        classification = None
        if samples:
            classification = classify_jesse_session(
                samples[-1], unchanged_observations=len(samples),
                stale_for_seconds=10**9, grace_seconds=60,
            )
        if classification and classification.state == "terminal_failure":
            item["reason"] = "terminal_failure_with_execution_evidence" if (
                classification.has_execution_evidence
            ) else "explicit_terminal_exception"
            category = (
                "stopped_sessions_that_executed"
                if classification.has_execution_evidence
                else "valid_strategy_or_harness_failures"
            )
            categories[category].append(item)
            continue
        if classification and classification.state in {
            "draft_not_started", "zombie_nonexecuting",
        } and not classification.has_execution_evidence:
            item["reason"] = classification.state
            categories["stopped_or_nonstarted_without_evidence"].append(item)
            continue
        blocker = " ".join((
            str(row["blocker_code"] or ""), str(row["blocker_detail"] or ""),
        )).lower()
        if any(marker in blocker for marker in _INFRASTRUCTURE_MARKERS):
            item["reason"] = "infrastructure_marker"
            categories["infrastructure_transport_failures"].append(item)
        elif any(marker in blocker for marker in (
            "jesse_execution_stopped", "harness", "order", "margin", "strategy",
        )) or row["error_text"]:
            item["reason"] = "strategy_or_harness_evidence"
            categories["valid_strategy_or_harness_failures"].append(item)
        else:
            item["reason"] = "insufficient_evidence_for_recovery"
            categories["irreducible_blockers"].append(item)
    return categories


def recover_zombie_execution_sessions(
    database: WorkflowDatabase,
    observations: Mapping[str, list[dict]],
    *,
    apply: bool = False,
    grace_seconds: float = 60,
    active_limit: int = 5,
) -> dict:
    """Invalidate only repeatedly observed non-executing sessions without evidence."""
    planned: list[dict] = []
    rejected: dict[str, str] = {}
    already_recovered: list[str] = []
    for session_id, samples in sorted(observations.items()):
        rows = database.rows(
            """SELECT w.id AS work_item_id,w.state,w.attempts,w.blocker_code,
                      d.session_id,d.state AS checkpoint_state
               FROM direct_execution_sessions d
               JOIN work_items w ON w.id=d.work_item_id
               WHERE d.session_id=?""",
            (session_id,),
        )
        recovery = database.rows(
            """SELECT work_item_id FROM direct_execution_recoveries
               WHERE old_session_id=?""",
            (session_id,),
        )
        if recovery:
            already_recovered.append(recovery[0]["work_item_id"])
            continue
        if not rows:
            rejected[session_id] = "checkpoint_not_found"
            continue
        row = rows[0]
        if len(samples) < 2:
            rejected[row["work_item_id"]] = "repeated_observation_required"
            continue
        signatures = []
        for sample in samples[-2:]:
            state = sample.get("state") if isinstance(sample.get("state"), dict) else {}
            results = state.get("results") if isinstance(state.get("results"), dict) else {}
            progress = results.get("progressbar")
            signatures.append((
                sample.get("status"), sample.get("updated_at") or sample.get("updatedAt"),
                results.get("executing"),
                progress.get("current") if isinstance(progress, dict) else results.get("progress"),
            ))
        if signatures[0] != signatures[1]:
            rejected[row["work_item_id"]] = "session_observation_changed"
            continue
        updated = samples[-1].get("updated_at") or samples[-1].get("updatedAt")
        updated_seconds = DirectMcpDispatcher._timestamp_seconds(updated)
        stale_for = (
            max(0.0, datetime.now(timezone.utc).timestamp() - updated_seconds)
            if updated_seconds is not None else 0.0
        )
        classification = classify_jesse_session(
            samples[-1], unchanged_observations=2,
            stale_for_seconds=stale_for, grace_seconds=grace_seconds,
        )
        if classification.state != "zombie_nonexecuting":
            rejected[row["work_item_id"]] = classification.state
            continue
        if classification.has_execution_evidence:
            rejected[row["work_item_id"]] = "session_has_execution_evidence"
            continue
        if database.rows(
            "SELECT 1 FROM runs WHERE work_item_id=? LIMIT 1",
            (row["work_item_id"],),
        ):
            rejected[row["work_item_id"]] = "durable_run_exists"
            continue
        if row["state"] not in {"blocked", "waiting_retry"}:
            rejected[row["work_item_id"]] = "work_item_not_recoverable"
            continue
        planned.append({
            "work_item_id": row["work_item_id"],
            "old_session_id": session_id,
            "old_state": classification.state,
            "transition": f"{row['state']}->scheduled",
            "attempts_reset": int(row["attempts"] or 0),
            "replacement_allowance": 1,
            "reason": "repeated unchanged non-executing Jesse session without evidence",
        })

    changed: list[str] = []
    if apply and planned:
        now = utc_now()
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in planned:
                work_item_id = item["work_item_id"]
                connection.execute(
                    """INSERT INTO direct_execution_recoveries(
                           work_item_id,old_session_id,old_state,reason,
                           replacement_allowed,created_at,updated_at
                       ) VALUES (?,?,?,?,1,?,?)""",
                    (
                        work_item_id, item["old_session_id"], item["old_state"],
                        item["reason"], now, now,
                    ),
                )
                deleted = connection.execute(
                    """DELETE FROM direct_execution_sessions
                       WHERE work_item_id=? AND session_id=?""",
                    (work_item_id, item["old_session_id"]),
                )
                changed_row = connection.execute(
                    """UPDATE work_items SET state='scheduled',attempts=0,
                              retry_after=NULL,blocker_code=NULL,blocker_detail=NULL,
                              claimed_by=NULL,claimed_at=NULL,updated_at=?
                       WHERE id=? AND state IN ('blocked','waiting_retry')""",
                    (now, work_item_id),
                )
                if deleted.rowcount != 1 or changed_row.rowcount != 1:
                    raise RuntimeError(
                        f"concurrent zombie recovery state change: {work_item_id}"
                    )
                connection.execute(
                    """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                           payload_json,occurred_at) VALUES(
                           'work_item',?,'zombie_execution_session_recovered',?,?)""",
                    (work_item_id, json.dumps({
                        "old_session_id": item["old_session_id"],
                        "old_state": item["old_state"],
                        "reason": item["reason"],
                        "attempts_reset": item["attempts_reset"],
                        "replacement_allowance": 1,
                    }, sort_keys=True), now),
                )
                changed.append(work_item_id)
        promoted = database.promote_scheduled_runnable(active_limit)
    else:
        promoted = 0
    return {
        "apply": apply, "planned": planned, "changed": changed,
        "already_recovered": sorted(already_recovered),
        "rejected": rejected, "promoted": promoted,
    }


def recover_executor_infrastructure_failures(
    database: WorkflowDatabase, *, apply: bool = False,
    worker_id: str = "ats-lab-supervisor", active_limit: int = 5,
) -> dict:
    """Replay durable evidence or requeue unexecuted significance transport failures."""
    rows = database.rows(
        """SELECT w.id,w.experiment_id,w.state,w.attempts,w.blocker_code,
                  w.blocker_detail,e.experiment_type,
                  r.id AS run_id,r.status AS run_status,
                  n.evidence_key
           FROM work_items w
           JOIN experiments e ON e.id=w.experiment_id
           LEFT JOIN runs r ON r.id=(
               SELECT rr.id FROM runs rr WHERE rr.work_item_id=w.id
                 AND rr.status='finished'
               ORDER BY COALESCE(rr.finished_at,rr.started_at) DESC,rr.id DESC
               LIMIT 1
           )
           LEFT JOIN normalized_evidence n ON n.run_id=r.id
           WHERE w.state='blocked' AND (
               w.blocker_code='analyzer_retry_exhausted'
               OR (w.blocker_code='retry_limit_reached'
                   AND w.blocker_detail LIKE '%invalid_executor_result%')
           ) ORDER BY w.id"""
    )
    replay = sorted({
        row["id"] for row in rows
        if row["run_status"] == "finished" and row["evidence_key"]
    })
    requeue = sorted({
        row["id"] for row in rows
        if not row["run_id"] and row["experiment_type"] == "significance"
    })
    skipped = sorted({row["id"] for row in rows} - set(replay) - set(requeue))
    changed: list[str] = []
    if apply and (replay or requeue):
        now = utc_now()
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item_id in replay:
                cursor = connection.execute(
                    """UPDATE work_items SET state='running',attempts=0,
                              retry_after=NULL,blocker_code='awaiting_batch_evaluation',
                              blocker_detail=?,claimed_by=?,claimed_at=?,updated_at=?
                       WHERE id=? AND state='blocked'""",
                    (f"RECOVERY-EXECUTOR-{item_id}", worker_id, now, now, item_id),
                )
                if cursor.rowcount:
                    changed.append(item_id)
            for item_id in requeue:
                cursor = connection.execute(
                    """UPDATE work_items SET state='scheduled',attempts=0,
                              retry_after=NULL,blocker_code=NULL,blocker_detail=NULL,
                              claimed_by=NULL,claimed_at=NULL,updated_at=?
                       WHERE id=? AND state='blocked'""",
                    (now, item_id),
                )
                if cursor.rowcount:
                    changed.append(item_id)
            for item_id in changed:
                connection.execute(
                    """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                           payload_json,occurred_at) VALUES(
                           'work_item',?,'executor_infrastructure_recovered',?,?)""",
                    (item_id, json.dumps({
                        "mode": "evidence_replay" if item_id in replay
                        else "execution_requeue",
                        "attempts_reset": True,
                    }, sort_keys=True), now),
                )
        promoted = database.promote_scheduled_runnable(active_limit)
    else:
        promoted = 0
    return {
        "apply": apply, "evidence_replay": replay,
        "execution_requeue": requeue, "skipped": skipped,
        "changed": sorted(changed), "promoted": promoted,
    }


def backfill_aggregate_route_coverage(
    database: WorkflowDatabase,
    *,
    apply: bool = False,
    policy: ResourcePolicy | None = None,
) -> dict:
    """Backfill only routes proven by matching finished Jesse session evidence."""
    policy = policy or ResourcePolicy()
    rows = database.rows(
        """SELECT r.id AS run_id,r.experiment_id,r.session_id,r.status,
                  r.route_json,r.metrics_json,r.raw_result_json,
                  e.specification_json
           FROM runs r JOIN experiments e ON e.id=r.experiment_id
           WHERE e.experiment_type='baseline' AND r.status='finished'
             AND r.route_json IS NULL
           ORDER BY COALESCE(r.finished_at,r.started_at),r.id"""
    )
    eligible: list[str] = []
    skipped: list[str] = []
    planned: list[tuple[dict, list[dict]]] = []
    for row in rows:
        specification = json.loads(row["specification_json"] or "{}")
        routes = specification.get("routes")
        metrics = json.loads(row["metrics_json"] or "{}")
        raw = json.loads(row["raw_result_json"] or "null")
        if not isinstance(routes, list) or len(routes) < 2:
            continue
        if not _finished_session_matches(
            raw, row["session_id"], metrics,
        ):
            skipped.append(row["run_id"])
            continue
        eligible.append(row["run_id"])
        planned.append((row, routes))

    updated: list[str] = []
    reevaluated: list[str] = []
    if apply:
        for row, routes in planned:
            coverage = _coverage(row["session_id"], routes)
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE runs SET route_json=? WHERE id=? AND route_json IS NULL",
                    (json.dumps(coverage, sort_keys=True), row["run_id"]),
                )
                database._refresh_run_evidence(connection, row["run_id"])
            updated.append(row["run_id"])
            if _reevaluate_route_failure(
                database, row["experiment_id"], row["run_id"],
                routes, coverage["routes"], policy,
            ):
                reevaluated.append(row["experiment_id"])
    return {
        "apply": apply,
        "eligible": eligible,
        "updated": updated,
        "reevaluated": reevaluated,
        "skipped_missing_session_evidence": skipped,
    }


def recover_partial_batch_retries(
    database: WorkflowDatabase,
    work_item_ids: Iterable[str],
    *,
    apply: bool = False,
    active_limit: int = 5,
) -> dict:
    """Reopen explicit members charged by one known batch-wide retry defect."""
    requested = sorted(set(work_item_ids))
    eligible: list[str] = []
    recovered_before: set[str] = set()
    rejected: dict[str, str] = {}
    for item_id in requested:
        rows = database.rows(
            """SELECT id,state,attempts,blocker_code,blocker_detail
               FROM work_items WHERE id=?""",
            (item_id,),
        )
        if not rows:
            rejected[item_id] = "unknown_work_item"
            continue
        row = rows[0]
        detail = row["blocker_detail"] or ""
        already_recovered = database.rows(
            """SELECT 1 FROM events
               WHERE aggregate_type='work_item' AND aggregate_id=?
                 AND event_type='partial_batch_retry_recovered' LIMIT 1""",
            (item_id,),
        )
        if already_recovered and row["state"] in {"ready", "scheduled"}:
            recovered_before.add(item_id)
            eligible.append(item_id)
            continue
        if (
            row["state"] != "blocked"
            or row["blocker_code"] != "retry_limit_reached"
            or not all(marker in detail for marker in _PARTIAL_BATCH_MARKERS)
        ):
            rejected[item_id] = "blocker_fingerprint_mismatch"
            continue
        eligible.append(item_id)

    recovered: list[str] = []
    if apply:
        now = utc_now()
        with database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item_id in eligible:
                cursor = connection.execute(
                    """UPDATE work_items SET state='scheduled',attempts=0,
                              retry_after=NULL,blocker_code=NULL,
                              blocker_detail=NULL,claimed_by=NULL,claimed_at=NULL,
                              updated_at=?
                       WHERE id=? AND (
                         (state='blocked' AND blocker_code='retry_limit_reached')
                         OR state IN ('ready','scheduled')
                       )""",
                    (now, item_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"concurrent recovery state change: {item_id}"
                    )
                if item_id not in recovered_before:
                    connection.execute(
                        """INSERT INTO events(
                               aggregate_type,aggregate_id,event_type,
                               payload_json,occurred_at
                           ) VALUES (
                               'work_item',?,'partial_batch_retry_recovered',?,?
                           )""",
                        (
                            item_id,
                            json.dumps({
                                "from": "blocked", "to": "scheduled",
                                "attempts_reset": True,
                                "reason": "batch-wide retry accounting defect",
                            }, sort_keys=True),
                            now,
                        ),
                    )
                recovered.append(item_id)
        promoted = database.promote_scheduled_runnable(active_limit)
    else:
        promoted = 0
    return {
        "apply": apply,
        "requested": requested,
        "eligible": eligible,
        "recovered": recovered,
        "promoted": promoted,
        "rejected": rejected,
    }


def recover_unexecuted_draft_checkpoint(
    database: WorkflowDatabase,
    work_item_id: str,
    session_id: str,
    session: dict,
    *,
    apply: bool = False,
    active_limit: int = 5,
) -> dict:
    """Archive one proven-unexecuted draft and permit one replacement session."""
    no_execution = (
        session.get("id") == session_id
        and session.get("status") == "draft"
        and session.get("metrics") is None
        and session.get("trades") == []
        and session.get("equity_curve") == []
        and session.get("execution_duration") is None
    )
    if not no_execution:
        return {"eligible": False, "reason": "session_has_execution_evidence"}
    rows = database.rows(
        """SELECT w.state,w.attempts,w.blocker_code,w.blocker_detail,
                  d.session_id,d.state AS checkpoint_state
           FROM work_items w
           LEFT JOIN direct_execution_sessions d ON d.work_item_id=w.id
           WHERE w.id=?""",
        (work_item_id,),
    )
    prior = database.rows(
        """SELECT 1 FROM events WHERE aggregate_type='work_item'
             AND aggregate_id=?
             AND event_type='unexecuted_draft_checkpoint_archived' LIMIT 1""",
        (work_item_id,),
    )
    if prior:
        return {"eligible": True, "transition": "already_recovered"}
    if not rows:
        return {"eligible": False, "reason": "unknown_work_item"}
    row = rows[0]
    fingerprint_ok = (
        row["state"] == "blocked"
        and row["blocker_code"] == "retry_limit_reached"
        and session_id in (row["blocker_detail"] or "")
        and row["session_id"] == session_id
        and row["checkpoint_state"] == "start_recovery_failed"
        and not database.rows(
            "SELECT 1 FROM runs WHERE work_item_id=? LIMIT 1", (work_item_id,),
        )
    )
    if not fingerprint_ok:
        return {"eligible": False, "reason": "checkpoint_fingerprint_mismatch"}
    if not apply:
        return {"eligible": True, "transition": "blocked->scheduled"}
    now = utc_now()
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        deleted = connection.execute(
            """DELETE FROM direct_execution_sessions
               WHERE work_item_id=? AND session_id=?
                 AND state='start_recovery_failed'""",
            (work_item_id, session_id),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("checkpoint changed during recovery")
        changed = connection.execute(
            """UPDATE work_items SET state='scheduled',attempts=0,retry_after=NULL,
                      blocker_code=NULL,blocker_detail=NULL,claimed_by=NULL,
                      claimed_at=NULL,updated_at=?
               WHERE id=? AND state='blocked' AND blocker_code='retry_limit_reached'""",
            (now, work_item_id),
        )
        if changed.rowcount != 1:
            raise RuntimeError("work item changed during recovery")
        connection.execute(
            """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                   payload_json,occurred_at) VALUES(
                   'work_item',?,'unexecuted_draft_checkpoint_archived',?,?)""",
            (work_item_id, json.dumps({
                "session_id": session_id, "session_status": "draft",
                "execution_evidence": False, "attempts_reset": True,
                "replacement_sessions_allowed": 1,
            }, sort_keys=True), now),
        )
    database.promote_scheduled_runnable(active_limit)
    state = database.rows(
        "SELECT state FROM work_items WHERE id=?", (work_item_id,),
    )[0]["state"]
    return {"eligible": True, "transition": f"blocked->{state}"}


def recover_verified_margin_sizing_blocker(
    database: WorkflowDatabase,
    work_item_id: str,
    *,
    strategy_name: str,
    apply: bool = False,
) -> dict:
    """Requeue one negative-margin failure without reusing its stopped session."""
    prior = database.rows(
        """SELECT 1 FROM events WHERE aggregate_type='work_item'
             AND aggregate_id=? AND event_type='margin_sizing_fix_recovered'
             LIMIT 1""",
        (work_item_id,),
    )
    rows = database.rows(
        """SELECT w.state,w.blocker_code,w.blocker_detail,e.specification_json,
                  d.session_id,d.state AS checkpoint_state,d.error_text
           FROM work_items w JOIN experiments e ON e.id=w.experiment_id
           LEFT JOIN direct_execution_sessions d ON d.work_item_id=w.id
           WHERE w.id=?""",
        (work_item_id,),
    )
    if not rows:
        return {"eligible": False, "reason": "unknown_work_item"}
    row = rows[0]
    valid_run = database.rows(
        """SELECT 1 FROM runs WHERE work_item_id=? AND status='finished'
             LIMIT 1""",
        (work_item_id,),
    )
    if valid_run:
        return {"eligible": False, "reason": "valid_run_exists"}
    if prior and row["session_id"] is None:
        return {"eligible": True, "transition": "already_recovered"}
    persisted_strategy = json.loads(row["specification_json"])["strategy_name"]
    initial_recovery = (
        not prior
        and row["state"] == "blocked"
        and row["blocker_code"] == "retry_limit_reached"
        and persisted_strategy == strategy_name
        and "Cannot submit an order with a value of $-" in (row["blocker_detail"] or "")
        and "available margin is $-" in (row["blocker_detail"] or "")
    )
    checkpoint_reconciliation = (
        bool(prior)
        and row["state"] in {"ready", "scheduled"}
        and persisted_strategy == strategy_name
        and row["checkpoint_state"] == "stopped"
        and "Cannot submit an order with a value of $-" in (row["error_text"] or "")
        and "available margin is $-" in (row["error_text"] or "")
    )
    checkpoint_valid = (
        row["session_id"] is None
        or (
            row["checkpoint_state"] == "stopped"
            and "Cannot submit an order with a value of $-" in (row["error_text"] or "")
            and "available margin is $-" in (row["error_text"] or "")
        )
    )
    if not ((initial_recovery and checkpoint_valid) or checkpoint_reconciliation):
        return {"eligible": False, "reason": "blocker_fingerprint_mismatch"}
    if not apply:
        transition = "ready_checkpoint->ready" if prior else "blocked->ready"
        return {"eligible": True, "transition": transition}
    now = utc_now()
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if row["session_id"] is not None:
            deleted = connection.execute(
                """DELETE FROM direct_execution_sessions
                   WHERE work_item_id=? AND session_id=? AND state='stopped'""",
                (work_item_id, row["session_id"]),
            )
            if deleted.rowcount != 1:
                raise RuntimeError("checkpoint changed during recovery")
            connection.execute(
                """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                       payload_json,occurred_at) VALUES(
                       'work_item',?,'invalid_stopped_checkpoint_archived',?,?)""",
                (work_item_id, json.dumps({
                    "session_id": row["session_id"],
                    "checkpoint_state": "stopped",
                    "valid_run_evidence": False,
                    "reason": "negative_margin_before_verified_sizing_fix",
                    "replacement_sessions_allowed": 1,
                }, sort_keys=True), now),
            )
        if initial_recovery:
            changed = connection.execute(
                """UPDATE work_items SET state='ready',attempts=0,retry_after=NULL,
                          blocker_code=NULL,blocker_detail=NULL,claimed_by=NULL,
                          claimed_at=NULL,updated_at=?
                   WHERE id=? AND state='blocked'
                     AND blocker_code='retry_limit_reached'""",
                (now, work_item_id),
            )
            if changed.rowcount != 1:
                raise RuntimeError("work item changed during recovery")
            connection.execute(
                """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                       payload_json,occurred_at) VALUES(
                       'work_item',?,'margin_sizing_fix_recovered',?,?)""",
                (work_item_id, json.dumps({
                    "strategy_name": strategy_name,
                    "change_scope": "sizing_only",
                    "attempts_reset": True,
                }, sort_keys=True), now),
            )
    transition = "ready_checkpoint->ready" if prior else "blocked->ready"
    return {"eligible": True, "transition": transition}


def _finished_session_matches(
    raw: object,
    session_id: str,
    metrics: dict,
) -> bool:
    if not isinstance(raw, dict):
        return False
    if set(raw) == {"session_id", "status", "metrics"}:
        session = raw
    else:
        data = raw.get("data")
        session = data.get("session") if isinstance(data, dict) else None
    return (
        isinstance(session, dict)
        and session.get("id", session.get("session_id")) == session_id
        and session.get("status") == "finished"
        and session.get("metrics") == metrics
    )


def _coverage(session_id: str, routes: list[dict]) -> dict:
    keys = ("exchange", "symbol", "timeframe", "start_date", "finish_date")
    return {
        "coverage": "aggregate_requested_routes",
        "evidence": {"session_id": session_id, "status": "finished"},
        "routes": [
            {key: route[key] for key in keys if key in route}
            for route in routes if isinstance(route, dict)
        ],
    }


def _reevaluate_route_failure(
    database: WorkflowDatabase,
    experiment_id: str,
    run_id: str,
    expected_routes: list[dict],
    observed_routes: list[dict],
    policy: ResourcePolicy,
) -> bool:
    evaluations = database.rows(
        """SELECT * FROM evaluations WHERE experiment_id=?
           ORDER BY evaluated_at DESC,id DESC LIMIT 1""",
        (experiment_id,),
    )
    if not evaluations:
        return False
    evaluation = evaluations[0]
    prior_summary = evaluation["summary"] or ""
    prior_metrics = evaluation["metrics_summary"] or ""
    if (
        "route" not in prior_summary.lower()
        and "failed=route_completion" not in prior_metrics
    ):
        return False
    evidence = database.normalized_evidence_for_run(run_id)
    gates = evaluate_gates(
        evidence, policy=policy, expected_routes=expected_routes,
        observed_routes=observed_routes,
    )
    corrected = [
        replace(item, verdict=gates.verdict, finding=gates.finding)
        for item in evidence
    ]
    summary = (
        "Persisted finished Jesse session covers all requested aggregate "
        f"routes. {gates.finding}"
    )
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        database._append_evaluation(
            connection,
            Evaluation(
                experiment_id=str(evaluation["experiment_id"]),
                verdict=gates.verdict,
                summary=summary,
                metrics_summary=json.dumps(
                    [item.to_compact_dict() for item in corrected],
                    separators=(",", ":"), sort_keys=True,
                ),
                next_step=(
                    "Continue controlled research validation."
                    if gates.verdict.value == "pass"
                    else "Resolve remaining deterministic gate findings."
                ),
                evaluator=str(evaluation["evaluator"]),
                evaluated_at=utc_now(),
            ),
        )
        database._refresh_run_evidence(connection, run_id)
    return True
