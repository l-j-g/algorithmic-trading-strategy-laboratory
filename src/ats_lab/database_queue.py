"""Work-item lifecycle operations: claims, transitions, retries, blockers."""

from __future__ import annotations

import json
import sqlite3
import uuid

from .models import Evaluation, WorkItem, WorkState, utc_now
from .retry_schedule import resolve_retry_after


def _ensure_matching_work_item(row: sqlite3.Row, item: WorkItem) -> None:
    conflicts = []
    if row["experiment_id"] != item.experiment_id:
        conflicts.append("experiment_id")
    if row["priority"] != item.priority:
        conflicts.append("priority")
    if json.loads(row["dependencies_json"] or "[]") != list(item.dependencies):
        conflicts.append("dependencies")
    if json.loads(row["specification_json"] or "{}") != dict(item.specification):
        conflicts.append("specification")
    if conflicts:
        raise ValueError(
            f"work item {item.id} already exists with different "
            f"{', '.join(conflicts)}"
        )



class QueueMixin:
    def upsert_work_item(self, item: WorkItem) -> dict:
        """Insert a work item or reconcile with the stored row.

        Re-registering an identical specification is a no-op that returns
        the stored row with its attempts and blocker bookkeeping intact. A
        differing experiment_id, priority, dependency list, or specification
        raises instead of silently overwriting queued work. A state change
        is applied only through the guarded transition machinery; anything
        beyond scheduled-to-ready is refused.
        """
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM work_items WHERE id=?", (item.id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO work_items(id, experiment_id, priority, state, dependencies_json,
                       attempts, retry_after, blocker_code, blocker_detail, specification_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (item.id, item.experiment_id, item.priority, item.state.value,
                     json.dumps(item.dependencies), item.attempts, item.retry_after, item.blocker_code,
                     item.blocker_detail, json.dumps(item.specification), now, now),
                )
                return dict(connection.execute(
                    "SELECT * FROM work_items WHERE id=?", (item.id,),
                ).fetchone())
            _ensure_matching_work_item(existing, item)
            if existing["state"] == item.state.value:
                return dict(existing)
            return self._transition_work_item_row(
                connection, existing, WorkState.READY,
                allowed_from=(WorkState.SCHEDULED,),
            )

    def claim_next(self, worker_id: str) -> dict | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM work_items
                   WHERE state = 'ready'
                     AND COALESCE(
                         json_extract(specification_json, '$.readiness.status'),
                         'ready'
                     ) != 'requirements_pending'
                   ORDER BY priority, created_at, id LIMIT 1"""
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            connection.execute(
                "UPDATE work_items SET state='running', claimed_by=?, claimed_at=?, updated_at=? WHERE id=? AND state='ready'",
                (worker_id, now, now, row["id"]),
            )
            return dict(connection.execute("SELECT * FROM work_items WHERE id=?", (row["id"],)).fetchone())

    def claim_batch(self, worker_id: str, limit: int) -> list[dict]:
        """Claim one ordered execution batch transactionally."""
        if limit < 1:
            raise ValueError("batch limit must be positive")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM work_items
                   WHERE state='ready'
                     AND COALESCE(
                         json_extract(specification_json, '$.readiness.status'),
                         'ready'
                     ) != 'requirements_pending'
                   ORDER BY CASE WHEN json_extract(
                       specification_json, '$.operation'
                   ) = 'hpo' THEN 0 ELSE 1 END,
                   priority,created_at,id LIMIT ?""",
                (limit,),
            ).fetchall()
            if not rows:
                return []
            now = utc_now()
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"""UPDATE work_items SET state='running',claimed_by=?,claimed_at=?,
                    blocker_code=NULL,blocker_detail=NULL,updated_at=?
                    WHERE state='ready' AND id IN ({placeholders})""",
                (worker_id, now, now, *ids),
            )
            return [
                dict(row) for row in connection.execute(
                    f"SELECT * FROM work_items WHERE id IN ({placeholders}) ORDER BY priority,created_at,id",
                    ids,
                ).fetchall()
            ]

    def mark_awaiting_evaluation(self, work_item_id: str, batch_id: str) -> None:
        """Keep a completed execution claimed until its batch evaluation is durable."""
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE work_items SET blocker_code='awaiting_batch_evaluation',
                   blocker_detail=?,updated_at=? WHERE id=? AND state='running'""",
                (batch_id, utc_now(), work_item_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"work item is not running: {work_item_id}")

    def pending_batch_evaluation(self, worker_id: str | None = None) -> list[dict]:
        """Return completed executions awaiting the isolated analysis turn."""
        query = """SELECT w.id AS work_item_id,w.experiment_id,w.blocker_detail AS batch_id,
                          e.specification_json AS experiment_json,
                          r.id AS run_id,r.session_id,r.status AS run_status,
                          r.route_json,r.dashboard_url,
                          r.metrics_json,r.error_json,r.started_at,r.finished_at
                   FROM work_items w
                   JOIN experiments e ON e.id=w.experiment_id
                   JOIN runs r ON r.work_item_id=w.id
                   WHERE w.state='running'
                     AND w.blocker_code='awaiting_batch_evaluation'
                     AND (w.retry_after IS NULL OR w.retry_after<=?)
                     AND r.id=(
                         SELECT latest.id FROM runs latest
                         WHERE latest.work_item_id=w.id
                         ORDER BY COALESCE(latest.finished_at,'' ) DESC,
                                  latest.rowid DESC LIMIT 1
                     )"""
        parameters: tuple = (utc_now(),)
        if worker_id:
            query += " AND w.claimed_by=?"
            parameters += (worker_id,)
        query += " ORDER BY w.blocker_detail,w.priority,w.created_at,w.id"
        return self.rows(query, parameters)

    def defer_batch_analysis_retry(
        self, work_item_id: str, *, blocker_code: str,
        blocker_detail: str, retry_after: str,
    ) -> dict:
        """Back off analyzer transport failures without releasing execution evidence."""
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {work_item_id}")
            if (
                row["state"] != WorkState.RUNNING.value
                or row["blocker_code"] != "awaiting_batch_evaluation"
            ):
                raise ValueError(
                    "work item is not awaiting batch evaluation: "
                    f"{work_item_id}"
                )
            connection.execute(
                "UPDATE work_items SET retry_after=?,updated_at=? WHERE id=?",
                (retry_after, now, work_item_id),
            )
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES('work_item',?,'analysis_retry_deferred',?,?)""",
                (
                    work_item_id,
                    json.dumps({
                        "blocker_code": blocker_code,
                        "detail": blocker_detail,
                        "retry_after": retry_after,
                        "attempt_charged": False,
                    }, sort_keys=True),
                    now,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,)
            ).fetchone())

    def archive_scheduled_dependents(
        self, work_item_id: str, *, reason: str,
    ) -> int:
        """Close obsolete children after parent execution cannot be evaluated."""
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT child.id FROM work_items child
                   JOIN json_each(child.dependencies_json) dependency
                     ON dependency.value=?
                   WHERE child.state='scheduled'""",
                (work_item_id,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """UPDATE work_items SET state='archived',blocker_code=NULL,
                       blocker_detail=NULL,updated_at=?
                       WHERE id=? AND state='scheduled'""",
                    (now, row["id"]),
                )
                connection.execute(
                    """INSERT INTO events(
                           aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                       ) VALUES('work_item',?,'dependent_archived',?,?)""",
                    (
                        row["id"],
                        json.dumps({
                            "dependency": work_item_id, "reason": reason,
                        }, sort_keys=True),
                        now,
                    ),
                )
            return len(rows)

    def requeue_finished_evaluation(
        self,
        work_item_id: str,
        *,
        worker_id: str,
        reason: str,
        batch_id: str | None = None,
    ) -> dict:
        """Reanalyze durable run evidence without repeating execution."""
        if not reason.strip():
            raise ValueError("requeue reason is required")
        now = utc_now()
        batch_id = batch_id or f"RECOVERY-{uuid.uuid4().hex[:12].upper()}"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {work_item_id}")
            if row["state"] != WorkState.FINISHED.value:
                raise ValueError(
                    f"cannot requeue evaluation for {work_item_id} from {row['state']}"
                )
            run = connection.execute(
                """SELECT id,metrics_json FROM runs
                   WHERE work_item_id=? AND status='finished'
                   ORDER BY finished_at DESC,id DESC LIMIT 1""",
                (work_item_id,),
            ).fetchone()
            if run is None:
                raise ValueError(
                    f"cannot requeue evaluation without finished run: {work_item_id}"
                )
            metrics = json.loads(run["metrics_json"] or "{}")
            if not metrics:
                raise ValueError(
                    f"cannot requeue evaluation without metrics: {work_item_id}"
                )
            connection.execute(
                """UPDATE work_items SET state='running',claimed_by=?,claimed_at=?,
                   blocker_code='awaiting_batch_evaluation',blocker_detail=?,
                   updated_at=? WHERE id=? AND state='finished'""",
                (worker_id, now, batch_id, now, work_item_id),
            )
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES ('work_item',?,'evaluation_requeued',?,?)""",
                (
                    work_item_id,
                    json.dumps({
                        "from": "finished",
                        "to": "running",
                        "batch_id": batch_id,
                        "run_id": run["id"],
                        "reason": reason,
                    }, sort_keys=True),
                    now,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,)
            ).fetchone())

    def finalize_batch_evaluation(
        self, evaluation: Evaluation, work_item_id: str | None = None,
    ) -> dict:
        """Persist one evaluation and finish its awaiting work item atomically.

        Pass work_item_id to finalize a specific execution; without it the
        most recently updated awaiting execution wins, and an ambiguous
        choice is recorded as an event so silent misattribution stays
        visible in the audit trail.
        """
        from .research_memory import enqueue_learning_safely

        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if work_item_id is not None:
                row = connection.execute(
                    """SELECT * FROM work_items WHERE id=? AND experiment_id=?
                       AND state='running'
                       AND blocker_code='awaiting_batch_evaluation'""",
                    (work_item_id, evaluation.experiment_id),
                ).fetchone()
                if row is None:
                    raise ValueError(
                        f"work item {work_item_id} is not awaiting batch "
                        f"evaluation for {evaluation.experiment_id}"
                    )
            else:
                candidates = connection.execute(
                    """SELECT * FROM work_items WHERE experiment_id=? AND state='running'
                       AND blocker_code='awaiting_batch_evaluation' ORDER BY updated_at DESC""",
                    (evaluation.experiment_id,),
                ).fetchall()
                if not candidates:
                    raise ValueError(f"no awaiting execution for {evaluation.experiment_id}")
                row = candidates[0]
                if len(candidates) > 1:
                    connection.execute(
                        """INSERT INTO events(aggregate_type,aggregate_id,event_type,payload_json,occurred_at)
                           VALUES ('experiment',?,'batch_finalization_ambiguous',?,?)""",
                        (evaluation.experiment_id,
                         json.dumps({
                             "candidates": [item["id"] for item in candidates],
                             "chosen": row["id"],
                         }),
                         now),
                    )
            self._append_evaluation(connection, evaluation)
            self._refresh_experiment_evidence(
                connection, evaluation.experiment_id,
            )
            enqueue_learning_safely(connection, evaluation)
            connection.execute(
                """UPDATE work_items SET state='finished',blocker_code=NULL,blocker_detail=NULL,
                   claimed_by=NULL,claimed_at=NULL,updated_at=? WHERE id=?""",
                (now, row["id"]),
            )
            connection.execute(
                """INSERT INTO events(aggregate_type,aggregate_id,event_type,payload_json,occurred_at)
                   VALUES ('work_item',?,'state_changed',?,?)""",
                (row["id"], json.dumps({"from": "running", "to": "finished"}), now),
            )
            return dict(connection.execute("SELECT * FROM work_items WHERE id=?", (row["id"],)).fetchone())

    def recover_stale_unexecuted_claims(
        self, claimed_before: str, *, apply: bool = False,
    ) -> dict:
        """Preview or recover stale claims that produced no run evidence."""
        query = """SELECT id,claimed_by,claimed_at FROM work_items
                   WHERE state='running' AND claimed_at<?
                     AND COALESCE(blocker_code,'')!='awaiting_batch_evaluation'
                   AND NOT EXISTS (
                         SELECT 1 FROM runs
                         WHERE runs.work_item_id=work_items.id
                           AND runs.status='finished'
                     )
                   ORDER BY claimed_at,id"""
        rows = self.rows(query, (claimed_before,))
        if apply and rows:
            ids = [row["id"] for row in rows]
            placeholders = ",".join("?" for _ in ids)
            with self.connect() as connection:
                now = utc_now()
                connection.execute(
                    f"""UPDATE work_items SET state='ready',claimed_by=NULL,claimed_at=NULL,
                        blocker_code='stale_claim_recovered',
                        blocker_detail='stale execution claim had no durable run evidence',
                        updated_at=? WHERE id IN ({placeholders}) AND state='running'""",
                    (now, *ids),
                )
                for work_item_id in ids:
                    connection.execute(
                        """INSERT INTO events(
                               aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                           ) VALUES ('work_item',?,'state_changed',?,?)""",
                        (work_item_id, json.dumps({
                            "from": "running", "to": "ready",
                            "reason": "stale_claim_recovered",
                        }), now),
                    )
        return {
            "claimed_before": claimed_before,
            "recoverable": rows,
            "applied": bool(apply and rows),
        }

    def resolve_blocked_work_item(
        self,
        work_item_id: str,
        *,
        resolution_code: str,
        detail: str,
        evidence_ids: list[str] | None = None,
    ) -> dict:
        """Reopen one fixed blocker while preserving durable resolution evidence."""
        if not resolution_code.strip():
            raise ValueError("resolution_code is required")
        if not detail.strip():
            raise ValueError("resolution detail is required")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {work_item_id}")
            if row["state"] != WorkState.BLOCKED.value:
                raise ValueError(
                    f"cannot resolve {work_item_id} from {row['state']}"
                )
            connection.execute(
                """UPDATE work_items SET state='ready',retry_after=NULL,
                   blocker_code=NULL,blocker_detail=NULL,claimed_by=NULL,
                   claimed_at=NULL,updated_at=? WHERE id=? AND state='blocked'""",
                (now, work_item_id),
            )
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES ('work_item',?,'blocker_resolved',?,?)""",
                (
                    work_item_id,
                    json.dumps({
                        "from": "blocked",
                        "to": "ready",
                        "previous_blocker_code": row["blocker_code"],
                        "resolution_code": resolution_code,
                        "detail": detail,
                        "evidence_ids": evidence_ids or [],
                    }, sort_keys=True),
                    now,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,)
            ).fetchone())

    def retry_blocked_work_item(
        self,
        work_item_id: str,
        *,
        reason: str = "operator requested retry",
        active_limit: int = 5,
    ) -> dict:
        """Requeue one blocked item while preserving its prior failure event."""
        reason = reason.strip()
        if not reason:
            raise ValueError("retry reason is required")
        if active_limit < 1:
            raise ValueError("active_limit must be positive")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {work_item_id}")
            if row["state"] != WorkState.BLOCKED.value:
                raise ValueError(
                    f"cannot retry {work_item_id} from {row['state']}"
                )
            connection.execute(
                """UPDATE work_items SET state='scheduled',attempts=0,
                   retry_after=NULL,blocker_code=NULL,blocker_detail=NULL,
                   claimed_by=NULL,claimed_at=NULL,updated_at=?
                   WHERE id=? AND state='blocked'""",
                (now, work_item_id),
            )
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES ('work_item',?,'blocker_retry_requested',?,?)""",
                (
                    work_item_id,
                    json.dumps({
                        "from": "blocked",
                        "to": "scheduled",
                        "previous_attempts": row["attempts"],
                        "previous_blocker_code": row["blocker_code"],
                        "previous_blocker_detail": row["blocker_detail"],
                        "reason": reason,
                        "attempts_reset": True,
                        "prior_runs_preserved": True,
                    }, sort_keys=True),
                    now,
                ),
            )
        promoted = self.promote_scheduled_runnable(active_limit)
        result = dict(self.rows(
            "SELECT * FROM work_items WHERE id=?", (work_item_id,)
        )[0])
        result["promoted"] = promoted
        return result

    def rectify_blocked_work_item(
        self,
        work_item_id: str,
        *,
        reason: str = "operator rectified historical blocker",
    ) -> dict:
        """Archive one blocker without executing it, preserving the audit trail."""
        reason = reason.strip()
        if not reason:
            raise ValueError("rectification reason is required")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {work_item_id}")
            if row["state"] != WorkState.BLOCKED.value:
                raise ValueError(
                    f"cannot rectify {work_item_id} from {row['state']}"
                )
            connection.execute(
                """UPDATE work_items SET state='archived',retry_after=NULL,
                   blocker_code='legacy_history',blocker_detail=?,
                   claimed_by=NULL,claimed_at=NULL,updated_at=?
                   WHERE id=? AND state='blocked'""",
                (f"Rectified by operator: {reason}", now, work_item_id),
            )
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES ('work_item',?,'blocker_rectified',?,?)""",
                (
                    work_item_id,
                    json.dumps({
                        "from": "blocked",
                        "to": "archived",
                        "previous_attempts": row["attempts"],
                        "previous_blocker_code": row["blocker_code"],
                        "previous_blocker_detail": row["blocker_detail"],
                        "reason": reason,
                        "execution_started": False,
                    }, sort_keys=True),
                    now,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,)
            ).fetchone())

    def promote_due_retries(self) -> int:
        """Return retryable work to ready state using lexicographic ISO-8601 UTC timestamps."""
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE work_items SET state='ready', retry_after=NULL, updated_at=?
                   WHERE state='waiting_retry' AND retry_after IS NOT NULL AND retry_after <= ?""",
                (now, now),
            )
            return cursor.rowcount

    def repair_relative_retry_schedules(self) -> int:
        """Convert legacy HTTP Retry-After seconds into comparable timestamps."""
        now = utc_now()
        repaired = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT id,retry_after FROM work_items
                   WHERE state='waiting_retry' AND retry_after IS NOT NULL"""
            ).fetchall()
            for row in rows:
                value = str(row["retry_after"]).strip()
                try:
                    float(value)
                except ValueError:
                    continue
                normalized = resolve_retry_after(
                    value, default_seconds=0,
                )
                connection.execute(
                    """UPDATE work_items SET retry_after=?,updated_at=?
                       WHERE id=? AND state='waiting_retry'""",
                    (normalized, now, row["id"]),
                )
                connection.execute(
                    """INSERT INTO events(
                           aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                       ) VALUES('work_item',?,'retry_schedule_normalized','{}',?)""",
                    (row["id"], now),
                )
                repaired += 1
        return repaired

    def defer_infrastructure_retry(
        self, work_item_id: str, *, blocker_code: str,
        blocker_detail: str, retry_after: str,
    ) -> dict:
        """Defer transport failure without charging strategy attempt budget."""
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {work_item_id}")
            if row["state"] != WorkState.RUNNING.value:
                raise ValueError(
                    f"cannot defer infrastructure retry from {row['state']}"
                )
            connection.execute(
                """UPDATE work_items SET state='waiting_retry',retry_after=?,
                          blocker_code=?,blocker_detail=?,claimed_by=NULL,
                          claimed_at=NULL,updated_at=? WHERE id=?""",
                (retry_after, blocker_code, blocker_detail, now, work_item_id),
            )
            connection.execute(
                """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                       payload_json,occurred_at) VALUES(
                       'work_item',?,'infrastructure_retry_deferred',?,?)""",
                (work_item_id, json.dumps({
                    "blocker_code": blocker_code,
                    "attempt_charged": False,
                }, sort_keys=True), now),
            )
            return dict(connection.execute(
                "SELECT * FROM work_items WHERE id=?", (work_item_id,),
            ).fetchone())

    def promote_scheduled_runnable(self, active_limit: int) -> int:
        """Fill ready capacity from scheduled work whose dependencies finished."""
        if active_limit < 1:
            raise ValueError("active_limit must be positive")
        self.reconcile_scheduled_dependencies()
        reconcile_hpo = getattr(self, "reconcile_hpo_validation_jobs", None)
        if reconcile_hpo is not None:
            reconcile_hpo()
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE state IN ('ready','running')"
            ).fetchone()[0]
            capacity = max(0, active_limit - int(active))
            if not capacity:
                return 0
            rows = connection.execute(
                """SELECT id FROM work_items
                   WHERE state='scheduled'
                     AND COALESCE(
                         json_extract(specification_json, '$.readiness.status'),
                         'ready'
                     ) != 'requirements_pending'
                     AND NOT EXISTS (
                         SELECT 1 FROM json_each(work_items.dependencies_json) dependency
                         LEFT JOIN work_items parent ON parent.id=dependency.value
                         WHERE parent.id IS NULL OR parent.state!='finished'
                     )
                   ORDER BY priority, created_at, id LIMIT ?""",
                (capacity,),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return 0
            placeholders = ",".join("?" for _ in ids)
            connection.execute(
                f"UPDATE work_items SET state='ready', updated_at=? "
                f"WHERE state='scheduled' AND id IN ({placeholders})",
                (now, *ids),
            )
            return len(ids)

    def reconcile_scheduled_dependencies(self, *, limit: int = 1000) -> dict[str, list[str]]:
        """Resolve scheduled children whose dependency state cannot progress.

        Missing or archived parents make a child impossible and archive it.
        Blocked parents block a child explicitly. Children blocked only by this
        dependency rule reopen to ``scheduled`` once every parent finishes.
        Repeat passes close dependency chains in one call.
        """
        if limit < 1:
            raise ValueError("limit must be positive")
        result = {"archived": [], "blocked": [], "released": []}
        while sum(len(ids) for ids in result.values()) < limit:
            changed = False
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    """SELECT child.id AS child_id,child.state AS child_state,
                              child.blocker_code,
                              dependency.value AS dependency_id,
                              parent.state AS dependency_state
                       FROM work_items child
                       JOIN json_each(child.dependencies_json) dependency
                       LEFT JOIN work_items parent
                         ON parent.id=dependency.value
                       WHERE child.state IN ('scheduled','blocked')
                       ORDER BY child.id,dependency.value"""
                ).fetchall()
                dependencies: dict[str, dict] = {}
                for row in rows:
                    item = dependencies.setdefault(row["child_id"], {
                        "state": row["child_state"],
                        "blocker_code": row["blocker_code"],
                        "missing": [],
                        "archived": [],
                        "blocked": [],
                        "unfinished": [],
                    })
                    dependency_id = row["dependency_id"]
                    dependency_state = row["dependency_state"]
                    if dependency_state is None:
                        item["missing"].append(dependency_id)
                    elif dependency_state == WorkState.ARCHIVED.value:
                        item["archived"].append(dependency_id)
                    elif dependency_state == WorkState.BLOCKED.value:
                        item["blocked"].append(dependency_id)
                    elif dependency_state != WorkState.FINISHED.value:
                        item["unfinished"].append(dependency_id)

                for child_id, item in dependencies.items():
                    if sum(len(ids) for ids in result.values()) >= limit:
                        break
                    row = connection.execute(
                        "SELECT * FROM work_items WHERE id=?", (child_id,)
                    ).fetchone()
                    if row is None:
                        continue
                    missing = item["missing"]
                    archived = item["archived"]
                    blocked = item["blocked"]
                    if missing or archived:
                        reason_code = (
                            "missing_dependency" if missing
                            else "dependency_archived"
                        )
                        detail = (
                            "Dependency work item is missing: "
                            + ", ".join(missing)
                            if missing else
                            "Dependency work item is archived: "
                            + ", ".join(archived)
                        )
                        self._transition_work_item_row(
                            connection, row, WorkState.ARCHIVED,
                            allowed_from=(WorkState.SCHEDULED, WorkState.BLOCKED),
                            blocker_code=reason_code,
                            blocker_detail=detail,
                        )
                        connection.execute(
                            """INSERT INTO events(
                                   aggregate_type,aggregate_id,event_type,
                                   payload_json,occurred_at
                               ) VALUES ('work_item',?,
                                         'dependency_reconciled',?,?)""",
                            (child_id, json.dumps({
                                "action": "archived",
                                "reason": reason_code,
                                "dependencies": missing or archived,
                            }, sort_keys=True), utc_now()),
                        )
                        result["archived"].append(child_id)
                        changed = True
                    elif blocked and row["state"] == WorkState.SCHEDULED.value:
                        detail = (
                            "Dependency work item is blocked: "
                            + ", ".join(blocked)
                        )
                        self._transition_work_item_row(
                            connection, row, WorkState.BLOCKED,
                            allowed_from=(WorkState.SCHEDULED,),
                            blocker_code="dependency_blocked",
                            blocker_detail=detail,
                        )
                        connection.execute(
                            """INSERT INTO events(
                                   aggregate_type,aggregate_id,event_type,
                                   payload_json,occurred_at
                               ) VALUES ('work_item',?,
                                         'dependency_reconciled',?,?)""",
                            (child_id, json.dumps({
                                "action": "blocked",
                                "reason": "dependency_blocked",
                                "dependencies": blocked,
                            }, sort_keys=True), utc_now()),
                        )
                        result["blocked"].append(child_id)
                        changed = True
                    elif (
                        row["state"] == WorkState.BLOCKED.value
                        and row["blocker_code"] == "dependency_blocked"
                        and not blocked
                        and not item["unfinished"]
                    ):
                        self._transition_work_item_row(
                            connection, row, WorkState.SCHEDULED,
                            allowed_from=(WorkState.BLOCKED,),
                        )
                        connection.execute(
                            """INSERT INTO events(
                                   aggregate_type,aggregate_id,event_type,
                                   payload_json,occurred_at
                               ) VALUES ('work_item',?,
                                         'dependency_reconciled',?,?)""",
                            (child_id, json.dumps({
                                "action": "released",
                                "reason": "dependencies_finished",
                            }, sort_keys=True), utc_now()),
                        )
                        result["released"].append(child_id)
                        changed = True
            if not changed:
                break
        return result

    def mark_unroutable_hpo_requirements_pending(self) -> int:
        """Keep HPO jobs without routes out of execution claims."""
        now = utc_now()
        changed = 0
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT w.id,w.specification_json,s.id AS study_id
                   FROM work_items w JOIN experiments e
                     ON e.id=w.experiment_id
                   LEFT JOIN hpo_studies s ON s.hpo_work_item_id=w.id
                   WHERE w.state IN ('scheduled','ready')
                     AND json_extract(w.specification_json,'$.operation')='hpo'
                     AND COALESCE(
                       json_array_length(json_extract(e.specification_json,'$.routes')),
                       0
                     )=0"""
            ).fetchall()
            for row in rows:
                specification = json.loads(row["specification_json"] or "{}")
                specification["readiness"] = {
                    "status": "requirements_pending",
                    "missing": ["hpo_routes"],
                }
                connection.execute(
                    """UPDATE work_items SET specification_json=?,
                              blocker_code='requirements_pending',
                              blocker_detail='HPO execution requires configured routes',
                              updated_at=? WHERE id=? AND state IN ('scheduled','ready')""",
                    (json.dumps(specification, sort_keys=True), now, row["id"]),
                    )
                if row["study_id"]:
                    connection.execute(
                        """UPDATE hpo_studies SET lifecycle_state='hpo_scheduled',
                           started_at=NULL,updated_at=?
                           WHERE id=? AND lifecycle_state='hpo_running'""",
                        (now, row["study_id"]),
                    )
                connection.execute(
                    """INSERT INTO events(
                           aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                       ) VALUES ('work_item',?,?,?,?)""",
                    (
                        row["id"], "readiness_pending",
                        json.dumps({"reason": "hpo_routes"}), now,
                    ),
                )
                changed += 1
        return changed

    def recover_abandoned_claims(self, worker_id: str, claimed_before: str) -> int:
        """Recover claims owned by an earlier process using the same worker ID."""
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE work_items SET state='ready', claimed_by=NULL, claimed_at=NULL,
                   blocker_code='abandoned_claim', blocker_detail='worker restarted after claim',
                   updated_at=? WHERE state='running' AND claimed_by=? AND claimed_at < ?""",
                (now, worker_id, claimed_before),
            )
            return cursor.rowcount

    @staticmethod
    def _transition_work_item_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        target: WorkState,
        *,
        allowed_from: tuple[WorkState, ...],
        blocker_code: str | None = None,
        blocker_detail: str | None = None,
        retry_after: str | None = None,
    ) -> dict:
        work_item_id = row["id"]
        if row["state"] == target.value:
            return dict(row)
        allowed = {state.value for state in allowed_from}
        if row["state"] not in allowed:
            raise ValueError(f"cannot transition {work_item_id} from {row['state']} to {target.value}")
        attempts = row["attempts"] + (1 if target is WorkState.WAITING_RETRY else 0)
        now = utc_now()
        connection.execute(
            """UPDATE work_items SET state=?, attempts=?, retry_after=?, blocker_code=?,
               blocker_detail=?, claimed_by=NULL, claimed_at=NULL, updated_at=? WHERE id=?""",
            (target.value, attempts, retry_after, blocker_code, blocker_detail, now, work_item_id),
        )
        connection.execute(
            "INSERT INTO events(aggregate_type, aggregate_id, event_type, payload_json, occurred_at) VALUES ('work_item', ?, 'state_changed', ?, ?)",
            (work_item_id, json.dumps({"from": row["state"], "to": target.value}), now),
        )
        return dict(connection.execute("SELECT * FROM work_items WHERE id=?", (work_item_id,)).fetchone())

    def transition_work_item(
        self,
        work_item_id: str,
        target: WorkState,
        *,
        allowed_from: tuple[WorkState, ...],
        blocker_code: str | None = None,
        blocker_detail: str | None = None,
        retry_after: str | None = None,
    ) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM work_items WHERE id=?", (work_item_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {work_item_id}")
            return self._transition_work_item_row(
                connection, row, target, allowed_from=allowed_from,
                blocker_code=blocker_code, blocker_detail=blocker_detail,
                retry_after=retry_after,
            )
