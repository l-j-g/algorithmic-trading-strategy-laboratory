"""SQLite storage and transactional queue operations."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
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
