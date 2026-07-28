"""SQLite storage and transactional queue operations."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from . import SCHEMA_VERSION
from .models import Evaluation, ExperimentSpec, RunResult, WorkItem, WorkState, utc_now


class WorkflowDatabase:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        schema = Path(__file__).with_name("schema.sql").read_text()
        with self.connect() as connection:
            connection.executescript(schema)
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )

    def upsert_experiment(self, spec: ExperimentSpec) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO strategies(name, created_at) VALUES (?, ?)",
                (spec.strategy_name or "unknown", now),
            )
            strategy_id = connection.execute("SELECT id FROM strategies WHERE name = ?", (spec.strategy_name or "unknown",)).fetchone()[0]
            connection.execute(
                """INSERT INTO experiments(id, strategy_id, experiment_type, hypothesis, archetype,
                   target_regime, failure_regime, specification_json, parent_experiment_id,
                   source_path, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET strategy_id=excluded.strategy_id,
                   experiment_type=excluded.experiment_type, hypothesis=excluded.hypothesis,
                   archetype=excluded.archetype, target_regime=excluded.target_regime,
                   failure_regime=excluded.failure_regime, specification_json=excluded.specification_json,
                   source_path=excluded.source_path, updated_at=excluded.updated_at""",
                (spec.id, strategy_id, spec.experiment_type.value, spec.hypothesis, spec.archetype,
                 spec.target_regime, spec.failure_regime, json.dumps(spec.to_dict(), default=str),
                 spec.parent_experiment_id, spec.source_path, now, now),
            )

    def upsert_work_item(self, item: WorkItem) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO work_items(id, experiment_id, priority, state, dependencies_json,
                   attempts, retry_after, blocker_code, blocker_detail, specification_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET priority=excluded.priority, state=excluded.state,
                   dependencies_json=excluded.dependencies_json, attempts=excluded.attempts,
                   retry_after=excluded.retry_after, blocker_code=excluded.blocker_code,
                   blocker_detail=excluded.blocker_detail, specification_json=excluded.specification_json,
                   updated_at=excluded.updated_at""",
                (item.id, item.experiment_id, item.priority, item.state.value,
                 json.dumps(item.dependencies), item.attempts, item.retry_after, item.blocker_code,
                 item.blocker_detail, json.dumps(item.specification), now, now),
            )

    def add_run(self, run: RunResult, source_path: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO runs(id, experiment_id, work_item_id, session_id, status, route_json,
                   dashboard_url, metrics_json, error_json, started_at, finished_at, source_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, dashboard_url=excluded.dashboard_url,
                   metrics_json=excluded.metrics_json, error_json=excluded.error_json,
                   finished_at=excluded.finished_at, source_path=excluded.source_path""",
                (run.id, run.experiment_id, run.work_item_id, run.session_id or None, run.status.value,
                 json.dumps(asdict(run.route)) if run.route else None, run.dashboard_url,
                 json.dumps(run.metrics) if run.metrics is not None else None,
                 json.dumps(run.error) if run.error is not None else None,
                 run.started_at, run.finished_at, source_path),
            )

    def add_evaluation(self, evaluation: Evaluation) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM evaluations WHERE experiment_id = ? AND evaluator = ?", (evaluation.experiment_id, evaluation.evaluator))
            connection.execute(
                """INSERT INTO evaluations(experiment_id, verdict, summary, metrics_summary,
                   next_step, evaluator, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (evaluation.experiment_id, evaluation.verdict.value, evaluation.summary,
                 evaluation.metrics_summary, evaluation.next_step, evaluation.evaluator,
                 evaluation.evaluated_at),
            )

    def add_run_and_evaluation(self, run: RunResult, evaluation: Evaluation, source_path: str = "") -> None:
        """Persist evidence and its research verdict in one transaction."""
        if run.experiment_id != evaluation.experiment_id:
            raise ValueError("run and evaluation experiment_id must match")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO runs(id, experiment_id, work_item_id, session_id, status, route_json,
                   dashboard_url, metrics_json, error_json, started_at, finished_at, source_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status, dashboard_url=excluded.dashboard_url,
                   metrics_json=excluded.metrics_json, error_json=excluded.error_json,
                   finished_at=excluded.finished_at, source_path=excluded.source_path""",
                (run.id, run.experiment_id, run.work_item_id, run.session_id or None, run.status.value,
                 json.dumps(asdict(run.route)) if run.route else None, run.dashboard_url,
                 json.dumps(run.metrics) if run.metrics is not None else None,
                 json.dumps(run.error) if run.error is not None else None,
                 run.started_at, run.finished_at, source_path),
            )
            connection.execute(
                "DELETE FROM evaluations WHERE experiment_id=? AND evaluator=?",
                (evaluation.experiment_id, evaluation.evaluator),
            )
            connection.execute(
                """INSERT INTO evaluations(experiment_id, verdict, summary, metrics_summary,
                   next_step, evaluator, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (evaluation.experiment_id, evaluation.verdict.value, evaluation.summary,
                 evaluation.metrics_summary, evaluation.next_step, evaluation.evaluator,
                 evaluation.evaluated_at),
            )

    def claim_next(self, worker_id: str) -> dict | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM work_items WHERE state = 'ready' ORDER BY priority, created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = utc_now()
            connection.execute(
                "UPDATE work_items SET state='running', claimed_by=?, claimed_at=?, updated_at=? WHERE id=? AND state='ready'",
                (worker_id, now, now, row["id"]),
            )
            return dict(connection.execute("SELECT * FROM work_items WHERE id=?", (row["id"],)).fetchone())

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

    def promote_scheduled_runnable(self, active_limit: int) -> int:
        """Fill ready capacity from scheduled work whose dependencies finished."""
        if active_limit < 1:
            raise ValueError("active_limit must be positive")
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

    def remaining_chain_count(self) -> int:
        """Count unresolved research chains, avoiding significance/baseline double-counting."""
        with self.connect() as connection:
            tracked = connection.execute(
                """SELECT COUNT(*) FROM synthesis_cohort_chains chain
                   WHERE EXISTS (
                       SELECT 1 FROM json_each(chain.work_item_ids_json) member
                       JOIN work_items w ON w.id=member.value
                       WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                   )"""
            ).fetchone()[0]
            untracked = connection.execute(
                """SELECT COUNT(DISTINCT w.experiment_id) FROM work_items w
                   WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                     AND NOT EXISTS (
                         SELECT 1 FROM synthesis_cohort_chains chain,
                              json_each(chain.work_item_ids_json) member
                         WHERE member.value=w.id
                     )"""
            ).fetchone()[0]
            return int(tracked) + int(untracked)

    def reserve_synthesis_cohort(
        self, *, worker_id: str, requested_count: int, low_watermark: int,
        lease_seconds: int, retry_cooldown_seconds: int,
    ) -> dict | None:
        """Acquire the single planner lease when unresolved chains reach the refill watermark."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat().replace("+00:00", "Z")
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        cooldown_start = (now - timedelta(seconds=retry_cooldown_seconds)).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE synthesis_cohorts SET status='failed',
                   failure_detail=COALESCE(failure_detail, 'planner lease expired'), updated_at=?
                   WHERE status='planning' AND lease_expires_at<=?""",
                (now_text, now_text),
            )
            if connection.execute(
                "SELECT 1 FROM synthesis_cohorts WHERE status='planning' LIMIT 1"
            ).fetchone():
                return None
            if connection.execute(
                """SELECT 1 FROM synthesis_cohorts
                   WHERE status='failed' AND updated_at>? LIMIT 1""",
                (cooldown_start,),
            ).fetchone():
                return None
            remaining = self._remaining_chain_count(connection)
            if remaining > low_watermark:
                return None
            cohort_id = f"COHORT-{uuid.uuid4().hex[:12].upper()}"
            connection.execute(
                """INSERT INTO synthesis_cohorts(
                       id,status,requested_count,remaining_at_trigger,planned_by,
                       lease_expires_at,created_at,updated_at
                   ) VALUES (?, 'planning', ?, ?, ?, ?, ?, ?)""",
                (cohort_id, requested_count, remaining, worker_id, lease_expires, now_text, now_text),
            )
            return {
                "id": cohort_id, "requested_count": requested_count,
                "remaining_at_trigger": remaining, "lease_expires_at": lease_expires,
            }

    @staticmethod
    def _remaining_chain_count(connection: sqlite3.Connection) -> int:
        tracked = connection.execute(
            """SELECT COUNT(*) FROM synthesis_cohort_chains chain
               WHERE EXISTS (
                   SELECT 1 FROM json_each(chain.work_item_ids_json) member
                   JOIN work_items w ON w.id=member.value
                   WHERE w.state IN ('scheduled','ready','running','waiting_retry')
               )"""
        ).fetchone()[0]
        untracked = connection.execute(
            """SELECT COUNT(DISTINCT w.experiment_id) FROM work_items w
               WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                 AND NOT EXISTS (
                     SELECT 1 FROM synthesis_cohort_chains chain,
                          json_each(chain.work_item_ids_json) member
                     WHERE member.value=w.id
                 )"""
        ).fetchone()[0]
        return int(tracked) + int(untracked)

    def activate_synthesis_cohort(self, cohort_id: str, chains: list[dict]) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,requested_count FROM synthesis_cohorts WHERE id=?", (cohort_id,)
            ).fetchone()
            if row is None or row["status"] != "planning":
                raise ValueError(f"cohort is not planning: {cohort_id}")
            if len(chains) != row["requested_count"]:
                raise ValueError(
                    f"cohort {cohort_id} requires {row['requested_count']} chains, got {len(chains)}"
                )
            for chain in chains:
                connection.execute(
                    """INSERT INTO synthesis_cohort_chains(
                           cohort_id,slot,lane,source_experiment_id,work_item_ids_json
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (cohort_id, chain["slot"], chain["lane"], chain.get("source_experiment_id"),
                     json.dumps(chain["work_item_ids"])),
                )
            connection.execute(
                """UPDATE synthesis_cohorts SET status='active', generated_count=?,
                   lease_expires_at=NULL, updated_at=? WHERE id=?""",
                (len(chains), now, cohort_id),
            )

    def fail_synthesis_cohort(self, cohort_id: str, detail: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE synthesis_cohorts SET status='failed', failure_detail=?,
                   lease_expires_at=NULL, updated_at=? WHERE id=? AND status='planning'""",
                (detail, utc_now(), cohort_id),
            )

    def refresh_synthesis_cohorts(self) -> int:
        """Mark cohorts drained when every member chain is terminal."""
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE synthesis_cohorts SET status='drained', updated_at=?
                   WHERE status='active' AND NOT EXISTS (
                       SELECT 1 FROM synthesis_cohort_chains chain
                       WHERE chain.cohort_id=synthesis_cohorts.id AND EXISTS (
                           SELECT 1 FROM json_each(chain.work_item_ids_json) member
                           JOIN work_items w ON w.id=member.value
                           WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                       )
                   )""",
                (utc_now(),),
            )
            return cursor.rowcount

    def synthesis_status(self) -> dict:
        rows = self.rows(
            """SELECT id,status,requested_count,generated_count,remaining_at_trigger,
                      planned_by,lease_expires_at,failure_detail,created_at,updated_at
               FROM synthesis_cohorts ORDER BY created_at DESC LIMIT 1"""
        )
        return {
            "remaining_chains": self.remaining_chain_count(),
            "latest_cohort": rows[0] if rows else None,
        }

    def reconcile_significance_gate(self, work_item_id: str, p_value: float, active_limit: int) -> dict:
        """Release or terminalize baselines dependent on completed significance work."""
        if p_value < 0.05:
            target = "ready"
            decision = "significance_passed"
        elif p_value <= 0.10:
            target = "archived"
            decision = "significance_inconclusive"
        else:
            target = "archived"
            decision = "significance_failed"
        now = utc_now()
        changed: list[str] = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            dependents = connection.execute(
                """SELECT id,specification_json FROM work_items
                   WHERE state='scheduled' AND EXISTS (
                       SELECT 1 FROM json_each(work_items.dependencies_json)
                       WHERE value=?
                   ) ORDER BY priority,created_at,id""",
                (work_item_id,),
            ).fetchall()
            active = connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE state IN ('ready','running')"
            ).fetchone()[0]
            for row in dependents:
                state = target
                if target == "ready" and int(active) >= active_limit:
                    state = "scheduled"
                elif state == "ready":
                    active += 1
                specification = json.loads(row["specification_json"])
                specification["gate_decision"] = (
                    decision if state != "scheduled" else "significance_passed_capacity_held"
                )
                connection.execute(
                    """UPDATE work_items SET state=?,specification_json=?,updated_at=?
                       WHERE id=? AND state='scheduled'""",
                    (state, json.dumps(specification, sort_keys=True), now, row["id"]),
                )
                changed.append(row["id"])
        return {"decision": decision, "dependents": changed}

    def execution_request(self, work_item_id: str) -> dict:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT w.id AS work_item_id, w.experiment_id, w.attempts, w.blocker_code,
                          w.blocker_detail, w.specification_json,
                          e.specification_json AS experiment_json
                   FROM work_items w JOIN experiments e ON e.id=w.experiment_id WHERE w.id=?""",
                (work_item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {work_item_id}")
            return {
                "schema_version": 1,
                "work_item_id": row["work_item_id"],
                "experiment_id": row["experiment_id"],
                "attempt": row["attempts"] + 1,
                "experiment": json.loads(row["experiment_json"]),
                "work_item": json.loads(row["specification_json"]),
                "prior_failure": ({
                    "code": row["blocker_code"], "detail": row["blocker_detail"],
                    "attempts": row["attempts"],
                } if row["attempts"] and row["blocker_code"] else None),
            }

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
            allowed = {state.value for state in allowed_from}
            if row["state"] == target.value:
                return dict(row)
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

    def rows(self, query: str, parameters: tuple = ()) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]
