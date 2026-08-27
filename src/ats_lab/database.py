"""SQLite storage and transactional queue operations."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import SCHEMA_VERSION
from .database_hpo import HpoMixin
from .database_synthesis import SynthesisMixin
from .database_evidence import EvidenceMixin
from .database_support import _json_object
from .database_queue import QueueMixin
from .models import (
    Evaluation,
    ExperimentSpec,
    RunResult,
    WorkItem,
    WorkState,
    utc_now,
)
from .retry_schedule import resolve_retry_after
from .strategy_dependencies import data_route_dicts, merge_data_routes



# A malformed synthesis response must get one immediate replacement attempt
# while unresolved chains remain. Two consecutive failures then honor the
# configured cooldown so a continuous supervisor cannot spin on the provider.
def _duration_ms(started_at: str, finished_at: str) -> int:
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return max(0, int((parse(finished_at) - parse(started_at)).total_seconds() * 1000))


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _ensure_columns(
    connection: sqlite3.Connection,
    table: str,
    additions: Mapping[str, str],
) -> None:
    """Guarded check-before-alter DDL; safe under concurrent initializers."""
    if not _table_exists(connection, table):
        return
    existing = _table_columns(connection, table)
    for name, declaration in additions.items():
        if name not in existing:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
            )


def _schema_statement(name: str) -> str:
    """Extract one canonical CREATE ... IF NOT EXISTS statement from schema.sql.

    Migrations rebuild objects from this single source of truth so migrated
    databases receive byte-identical definitions to fresh ones.
    """
    text = Path(__file__).with_name("schema.sql").read_text()
    pattern = re.compile(
        rf"CREATE\s+(?:TABLE|VIEW|INDEX|TRIGGER)\s+IF\s+NOT\s+EXISTS\s+"
        rf"{re.escape(name)}\b",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if match is None:
        raise KeyError(f"schema.sql does not define: {name}")
    cursor = match.start()
    quote = ""
    while cursor < len(text):
        character = text[cursor]
        if quote:
            if character == quote:
                quote = ""
        elif character in ("'", '"'):
            quote = character
        elif character == ";":
            break
        cursor += 1
    return text[match.start():cursor]


def _migrate_runs_raw_result(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "runs", {"raw_result_json": "TEXT"})


def _migrate_execution_checkpoints(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "direct_execution_sessions", {
        "first_observed_at": "TEXT",
        "last_observed_at": "TEXT",
        "last_jesse_updated_at": "TEXT",
        "last_progress": "REAL",
        "unchanged_observations": "INTEGER NOT NULL DEFAULT 0",
        "recovery_attempted": "INTEGER NOT NULL DEFAULT 0",
        "replacement_created": "INTEGER NOT NULL DEFAULT 0",
    })


def _migrate_evidence_protocol_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "normalized_evidence", {
        "monte_carlo_scenarios": "INTEGER",
        "monte_carlo_method": "TEXT",
        "walk_forward_windows": "INTEGER",
        "walk_forward_method": "TEXT",
    })


def _migrate_evidence_leverage_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "normalized_evidence", {
        "leverage_mode": "TEXT",
        "configured_futures_leverage": "REAL",
        "effective_leverage_mean": "REAL",
        "effective_leverage_p95": "REAL",
        "effective_leverage_max": "REAL",
        "liquidation_count": "INTEGER",
    })


def _migrate_normalized_evidence_backfill(connection: sqlite3.Connection) -> None:
    if _table_exists(connection, "runs") and _table_exists(
        connection, "experiments",
    ):
        WorkflowDatabase._backfill_normalized_evidence(
            connection, record_event=False,
        )


def _migrate_evidence_mc_tail_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "normalized_evidence", {
        "monte_carlo_best_5pct_net_profit_percentage": "REAL",
        "monte_carlo_worst_5pct_net_profit_percentage": "REAL",
    })
    if _table_exists(connection, "runs") and _table_exists(
        connection, "experiments",
    ):
        WorkflowDatabase._backfill_normalized_evidence(
            connection, record_event=False,
        )


def _migrate_evaluations_append_only(connection: sqlite3.Connection) -> None:
    """Rebuild evaluations as append-only evaluation_history plus a view.

    Verdict revisions are preserved with a per-(experiment_id, evaluator)
    sequence; every revision except the newest per group is marked superseded
    so the ``evaluations`` view keeps exposing exactly the rows all existing
    readers expect. The candidate_summary view is dropped here because it
    referenced the old table; the baseline schema recreates it against the
    new view right after migrations run.
    """
    if not _table_exists(connection, "evaluations") or _table_exists(
        connection, "evaluation_history",
    ):
        return
    now = utc_now()
    connection.execute(_schema_statement("evaluation_history"))
    connection.execute(
        """INSERT INTO evaluation_history(
               id,experiment_id,verdict,summary,metrics_summary,next_step,
               gate_results_json,evaluator,evaluated_at,sequence,superseded_at)
           SELECT id,experiment_id,verdict,summary,metrics_summary,next_step,
                  gate_results_json,evaluator,evaluated_at,
                  (
                      SELECT COUNT(*) FROM evaluations older
                      WHERE older.experiment_id=e.experiment_id
                        AND older.evaluator=e.evaluator
                        AND older.id<=e.id
                  ) - 1,
                  CASE WHEN EXISTS (
                      SELECT 1 FROM evaluations newer
                      WHERE newer.experiment_id=e.experiment_id
                        AND newer.evaluator=e.evaluator
                        AND newer.id>e.id
                  ) THEN ? ELSE NULL END
           FROM evaluations e""",
        (now,),
    )
    connection.execute("DROP TABLE evaluations")
    connection.execute("DROP VIEW IF EXISTS candidate_summary")
    connection.execute(_schema_statement("evaluations"))


# Ordered migrations are the single versioning mechanism. Each entry must be
# idempotent and tolerate a fresh database where baseline tables do not exist
# yet; initialize() applies the baseline schema.sql after pending migrations.
_MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (2, _migrate_runs_raw_result),
    (3, _migrate_execution_checkpoints),
    (4, _migrate_evidence_protocol_columns),
    (5, _migrate_evidence_leverage_columns),
    (6, _migrate_normalized_evidence_backfill),
    (7, _migrate_evaluations_append_only),
    (8, _migrate_evidence_mc_tail_columns),
)

if SCHEMA_VERSION != _MIGRATIONS[-1][0]:
    raise RuntimeError(
        "SCHEMA_VERSION must equal the latest ordered migration version"
    )



class WorkflowDatabase(QueueMixin, HpoMixin, SynthesisMixin, EvidenceMixin):
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create or upgrade the database schema.

        Concurrency contract: concurrent ``initialize()`` callers serialize on
        the SQLite writer lock (``busy_timeout`` plus ``BEGIN IMMEDIATE``) and
        every migration step uses guarded, idempotent DDL, so racing
        initializers converge on the same schema instead of failing on
        duplicate ALTERs. Pending migrations run first; the baseline
        ``schema.sql`` then fills in any objects a fresh database needs.
        """
        schema = Path(__file__).with_name("schema.sql").read_text()
        with self.connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       version INTEGER PRIMARY KEY,
                       applied_at TEXT NOT NULL
                   )"""
            )
            connection.execute("BEGIN IMMEDIATE")
            applied = {
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations"
                )
            }
            for version, migrate in _MIGRATIONS:
                if version in applied:
                    continue
                migrate(connection)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
            connection.executescript(schema)

    def record_work_item_stage(
        self,
        work_item_id: str,
        stage: str,
        started_at: str,
        *,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        state: str = "completed",
        analyzer_attempt: int | None = None,
        cohort_id: str | None = None,
        outcome: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> dict:
        """Persist one execution/analyzer stage timing."""
        if not stage.strip():
            raise ValueError("stage is required")
        if duration_ms is None and finished_at:
            duration_ms = _duration_ms(started_at, finished_at)
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO work_item_stage_timings(
                       work_item_id,stage,state,analyzer_attempt,cohort_id,
                       started_at,finished_at,duration_ms,outcome,detail_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    work_item_id, stage, state, analyzer_attempt, cohort_id,
                    started_at, finished_at, duration_ms, outcome,
                    json.dumps(detail or {}, sort_keys=True),
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM work_item_stage_timings WHERE id=?",
                (cursor.lastrowid,),
            ).fetchone())

    def start_work_item_stage(
        self,
        work_item_id: str,
        stage: str,
        *,
        analyzer_attempt: int | None = None,
        cohort_id: str | None = None,
        started_at: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> dict:
        return self.record_work_item_stage(
            work_item_id, stage, started_at or utc_now(),
            state="running", analyzer_attempt=analyzer_attempt,
            cohort_id=cohort_id, detail=detail,
        )

    def finish_work_item_stage(
        self,
        timing_id: int,
        *,
        state: str = "completed",
        outcome: str | None = None,
        finished_at: str | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> dict:
        finished_at = finished_at or utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM work_item_stage_timings WHERE id=?",
                (timing_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown stage timing: {timing_id}")
            connection.execute(
                """UPDATE work_item_stage_timings
                   SET state=?,finished_at=?,duration_ms=?,outcome=?,detail_json=?
                   WHERE id=?""",
                (
                    state, finished_at,
                    _duration_ms(row["started_at"], finished_at),
                    outcome,
                    json.dumps(detail or json.loads(row["detail_json"] or "{}"),
                               sort_keys=True),
                    timing_id,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM work_item_stage_timings WHERE id=?",
                (timing_id,),
            ).fetchone())

    def work_item_stage_timings(
        self,
        work_item_id: str | None = None,
        *,
        limit: int = 100,
    ) -> list[dict]:
        limit = max(1, min(int(limit), 5000))
        query = "SELECT * FROM work_item_stage_timings"
        parameters: tuple[object, ...]
        if work_item_id:
            query += " WHERE work_item_id=?"
            parameters = (work_item_id, limit)
        else:
            parameters = (limit,)
        query += " ORDER BY started_at DESC,id DESC LIMIT ?"
        rows = self.rows(query, parameters)
        result = []
        for row in rows:
            attempt = row.pop("analyzer_attempt")
            completed_at = row.pop("finished_at")
            duration_ms = row.pop("duration_ms")
            detail = json.loads(row.pop("detail_json") or "{}")
            result.append({
                **row,
                "attempt": attempt,
                "completed_at": completed_at,
                "duration_seconds": (
                    duration_ms / 1000 if duration_ms is not None else None
                ),
                "detail": detail,
            })
        return result

    def recent_stage_timings(self, limit: int = 100) -> list[dict]:
        return self.work_item_stage_timings(limit=limit)

    def control_status(self) -> dict:
        """Return durable operator intent."""
        rows = self.rows(
            "SELECT desired_state,updated_at,updated_by FROM operator_control WHERE id=1"
        )
        if not rows:
            raise RuntimeError("operator control is not initialized")
        return rows[0]

    def record_event(
        self,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        occurred_at: str | None = None,
    ) -> dict:
        """Append one structured operator activity event."""
        if not aggregate_type.strip():
            raise ValueError("event aggregate_type is required")
        if not aggregate_id.strip():
            raise ValueError("event aggregate_id is required")
        if not event_type.strip():
            raise ValueError("event event_type is required")
        timestamp = occurred_at or utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES (?,?,?,?,?)""",
                (
                    aggregate_type, aggregate_id, event_type,
                    json.dumps(dict(payload or {}), sort_keys=True, default=str),
                    timestamp,
                ),
            )
            row = connection.execute(
                "SELECT * FROM events WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            return dict(row)

    def events_after(self, event_id: int = 0, *, limit: int = 100) -> list[dict]:
        """Return bounded durable events after an inclusive display cursor."""
        limit = max(1, min(int(limit), 5000))
        return self.rows(
            """SELECT id,aggregate_type,aggregate_id,event_type,payload_json,occurred_at
               FROM events WHERE id>? ORDER BY id ASC LIMIT ?""",
            (max(0, int(event_id)), limit),
        )

    def latest_event_id(self) -> int:
        rows = self.rows("SELECT COALESCE(MAX(id),0) AS id FROM events")
        return int(rows[0]["id"]) if rows else 0

    def set_control_state(
        self, desired_state: str, *, updated_by: str = "operator",
    ) -> dict:
        """Set durable supervisor intent and record the transition."""
        if desired_state not in {"running", "paused", "stop_requested"}:
            raise ValueError(f"invalid control state: {desired_state}")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT desired_state FROM operator_control WHERE id=1"
            ).fetchone()
            if previous is None:
                raise RuntimeError("operator control is not initialized")
            connection.execute(
                """UPDATE operator_control
                   SET desired_state=?,updated_at=?,updated_by=? WHERE id=1""",
                (desired_state, now, updated_by),
            )
            if previous["desired_state"] != desired_state:
                connection.execute(
                    """INSERT INTO events(
                           aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                       ) VALUES ('supervisor','control','control_changed',?,?)""",
                    (json.dumps({
                        "from": previous["desired_state"],
                        "to": desired_state,
                        "updated_by": updated_by,
                    }), now),
                )
            return dict(connection.execute(
                """SELECT desired_state,updated_at,updated_by
                   FROM operator_control WHERE id=1"""
            ).fetchone())

    def update_supervisor_runtime(
        self,
        *,
        worker_id: str,
        process_id: int,
        phase: str,
        started_at: str,
        batch_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict:
        """Publish current supervisor phase for terminal monitoring."""
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO supervisor_runtime(
                       id,worker_id,process_id,phase,batch_id,detail_json,
                       started_at,heartbeat_at
                   ) VALUES (1,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       worker_id=excluded.worker_id,
                       process_id=excluded.process_id,
                       phase=excluded.phase,
                       batch_id=excluded.batch_id,
                       detail_json=excluded.detail_json,
                       started_at=excluded.started_at,
                       heartbeat_at=excluded.heartbeat_at""",
                (
                    worker_id, process_id, phase, batch_id,
                    json.dumps(detail or {}, sort_keys=True), started_at, now,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM supervisor_runtime WHERE id=1"
            ).fetchone())

    def supervisor_runtime_status(self) -> dict | None:
        rows = self.rows("SELECT * FROM supervisor_runtime WHERE id=1")
        if not rows:
            return None
        result = rows[0]
        result["detail"] = json.loads(result.pop("detail_json") or "{}")
        return result

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
            self._refresh_experiment_evidence(connection, spec.id)

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
            experiment = json.loads(row["experiment_json"])
            work_item = json.loads(row["specification_json"])
            effective_data_routes = merge_data_routes(
                str(experiment.get("strategy_name") or ""),
                experiment.get("routes")
                if isinstance(experiment.get("routes"), list) else (),
                experiment.get("data_routes"),
                work_item.get("data_routes"),
            )
            if effective_data_routes:
                route_dicts = data_route_dicts(effective_data_routes)
                experiment["data_routes"] = route_dicts
                work_item["data_routes"] = route_dicts
            request = {
                "schema_version": 1,
                "work_item_id": row["work_item_id"],
                "experiment_id": row["experiment_id"],
                "attempt": row["attempts"] + 1,
                "experiment": experiment,
                "work_item": work_item,
                "prior_failure": ({
                    "code": row["blocker_code"], "detail": row["blocker_detail"],
                    "attempts": row["attempts"],
                } if row["attempts"] and row["blocker_code"] else None),
            }
            trial_id = work_item.get("hpo_trial_id")
            if trial_id:
                trial = connection.execute(
                    "SELECT params_json FROM hpo_trials WHERE id=?",
                    (trial_id,),
                ).fetchone()
                if trial is None:
                    raise ValueError(
                        f"unknown HPO trial for validation: {trial_id}"
                    )
                parameters = json.loads(trial["params_json"] or "{}")
                if not isinstance(parameters, dict):
                    raise ValueError(
                        f"invalid HPO parameters for validation: {trial_id}"
                    )
                request["execution_context"] = {
                    "optimizer_parameters": parameters,
                }
            return request

    def rows(self, query: str, parameters: tuple = ()) -> list[dict]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]
