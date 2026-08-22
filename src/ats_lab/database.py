"""SQLite storage and transactional queue operations."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import SCHEMA_VERSION
from .evidence import (
    NormalizedEvidence,
    evidence_key,
    normalize_run_evidence,
)
from .database_hpo import HpoMixin
from .database_support import _json_object
from .database_queue import QueueMixin
from .models import (
    Evaluation,
    ExperimentSpec,
    RouteSpec,
    RunResult,
    WorkItem,
    WorkState,
    utc_now,
)
from .retry_schedule import resolve_retry_after


_EVIDENCE_COLUMNS = tuple(NormalizedEvidence.__dataclass_fields__)
_EVIDENCE_FILTERS = frozenset(_EVIDENCE_COLUMNS)

# A malformed synthesis response must get one immediate replacement attempt
# while unresolved chains remain. Two consecutive failures then honor the
# configured cooldown so a continuous supervisor cannot spin on the provider.
_MAX_RECENT_SYNTHESIS_FAILURES = 2


def _route_payload(
    route: RouteSpec | dict[str, Any],
) -> dict[str, Any]:
    return dict(route) if isinstance(route, dict) else asdict(route)


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



class WorkflowDatabase(QueueMixin, HpoMixin):
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

    def upsert_normalized_evidence(
        self,
        evidence: NormalizedEvidence,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        """Persist one canonical row. Returns inserted, updated, or skipped."""
        if connection is not None:
            return self._upsert_normalized_evidence(connection, evidence)
        with self.connect() as owned:
            return self._upsert_normalized_evidence(owned, evidence)

    def normalized_evidence_for_run(
        self, run_id: str,
    ) -> list[NormalizedEvidence]:
        """Return every atomic route/split persisted for one raw run."""
        return self.query_normalized_evidence({"run_id": run_id})

    def normalized_evidence_for_experiment(
        self, experiment_id: str,
    ) -> list[NormalizedEvidence]:
        return self.query_normalized_evidence({"experiment_id": experiment_id})

    def query_normalized_evidence(
        self,
        filters: Mapping[str, object] | None = None,
        *,
        limit: int = 500,
    ) -> list[NormalizedEvidence]:
        """Typed normalized query. Only canonical field names are accepted."""
        filters = dict(filters or {})
        unknown = sorted(set(filters) - _EVIDENCE_FILTERS)
        if unknown:
            raise ValueError(
                "unknown normalized evidence filters: " + ", ".join(unknown)
            )
        limit = max(1, min(int(limit), 5000))
        clauses = []
        values: list[object] = []
        for field, value in filters.items():
            if hasattr(value, "value"):
                value = value.value
            if value is None:
                clauses.append(f"{field} IS NULL")
            else:
                clauses.append(f"{field}=?")
                values.append(value)
        query = "SELECT * FROM normalized_evidence"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += (
            " ORDER BY COALESCE(completed_at,'') DESC,"
            "experiment_id,run_id,session_id,evidence_key LIMIT ?"
        )
        values.append(limit)
        return [
            NormalizedEvidence.from_row(row)
            for row in self.rows(query, tuple(values))
        ]

    def compatible_evidence(
        self,
        anchor: NormalizedEvidence,
        *,
        limit: int = 500,
    ) -> list[NormalizedEvidence]:
        """Return exact symbol/timeframe/period/split peers."""
        return self.query_normalized_evidence({
            "symbol": anchor.symbol,
            "timeframe": anchor.timeframe,
            "start_date": anchor.start_date,
            "finish_date": anchor.finish_date,
            "evidence_split": anchor.evidence_split,
        }, limit=limit)

    def diagnostic_raw_evidence(self, run_id: str) -> dict | None:
        """Explicit diagnostic-only access to retained raw evidence."""
        rows = self.rows(
            """SELECT id AS run_id,experiment_id,work_item_id,session_id,status,
                      route_json,dashboard_url,metrics_json,raw_result_json,error_json,
                      started_at,finished_at,source_path
               FROM runs WHERE id=?""",
            (run_id,),
        )
        if not rows:
            return None
        row = rows[0]
        for source, target in (
            ("route_json", "route"),
            ("metrics_json", "metrics"),
            ("raw_result_json", "raw_result"),
            ("error_json", "error"),
        ):
            raw = row.pop(source)
            try:
                row[target] = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                row[target] = {"invalid_json": raw}
        return row

    def backfill_normalized_evidence(self) -> dict[str, int]:
        """Idempotently normalize retained legacy run evidence."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._backfill_normalized_evidence(connection)

    @staticmethod
    def _backfill_normalized_evidence(
        connection: sqlite3.Connection,
        *,
        record_event: bool = True,
    ) -> dict[str, int]:
        result = {
            "scanned": 0,
            "inserted": 0,
            "updated": 0,
            "skipped": 0,
            "invalid": 0,
        }
        run_ids = [
            row["id"] for row in connection.execute(
                "SELECT id FROM runs ORDER BY id"
            ).fetchall()
        ]
        for run_id in run_ids:
            result["scanned"] += 1
            try:
                outcomes = WorkflowDatabase._refresh_run_evidence(
                    connection, run_id,
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                result["invalid"] += 1
                continue
            for outcome in outcomes:
                result[outcome] += 1
        if record_event:
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES ('evidence','normalized','evidence_backfilled',?,?)""",
                (json.dumps(result, sort_keys=True), utc_now()),
            )
        return result

    @staticmethod
    def _upsert_normalized_evidence(
        connection: sqlite3.Connection,
        evidence: NormalizedEvidence,
    ) -> str:
        key = evidence_key(evidence)
        existing = connection.execute(
            "SELECT * FROM normalized_evidence WHERE evidence_key=?",
            (key,),
        ).fetchone()
        if (
            existing is not None
            and NormalizedEvidence.from_row(existing).to_dict() == evidence.to_dict()
        ):
            return "skipped"
        payload = evidence.to_dict()
        columns = ("evidence_key", *_EVIDENCE_COLUMNS, "updated_at")
        values = (
            key,
            *(payload[column] for column in _EVIDENCE_COLUMNS),
            utc_now(),
        )
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{column}=excluded.{column}"
            for column in (*_EVIDENCE_COLUMNS, "updated_at")
        )
        connection.execute(
            f"""INSERT INTO normalized_evidence({",".join(columns)})
                VALUES ({placeholders})
                ON CONFLICT(evidence_key) DO UPDATE SET {updates}""",
            values,
        )
        return "inserted" if existing is None else "updated"

    @staticmethod
    def _refresh_run_evidence(
        connection: sqlite3.Connection,
        run_id: str,
    ) -> list[str]:
        row = connection.execute(
            """SELECT r.*,e.experiment_type,e.specification_json,
                      w.specification_json AS work_specification_json,
                      s.name AS strategy,ev.verdict,ev.summary,ev.next_step
               FROM runs r
               JOIN experiments e ON e.id=r.experiment_id
               LEFT JOIN work_items w ON w.id=r.work_item_id
               LEFT JOIN strategies s ON s.id=e.strategy_id
               LEFT JOIN evaluations ev ON ev.id=(
                   SELECT latest.id FROM evaluations latest
                   WHERE latest.experiment_id=e.id
                   ORDER BY latest.evaluated_at DESC,latest.id DESC LIMIT 1
               )
               WHERE r.id=?""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown run: {run_id}")
        spec = {
            **_json_object(row["specification_json"]),
            **_json_object(row["work_specification_json"]),
        }
        route = _json_object(row["route_json"])
        metrics = _json_object(row["metrics_json"])
        normalized = normalize_run_evidence(
            experiment_id=row["experiment_id"],
            run_id=row["id"],
            session_id=row["session_id"],
            strategy=row["strategy"],
            lifecycle_stage=row["experiment_type"],
            experiment_spec=spec,
            route=route,
            metrics=metrics,
            completed_at=row["finished_at"],
            verdict=row["verdict"],
            finding=row["summary"],
            next_action=row["next_step"],
        )
        expected_keys = {evidence_key(item) for item in normalized}
        existing_keys = {
            existing["evidence_key"]
            for existing in connection.execute(
                "SELECT evidence_key FROM normalized_evidence WHERE run_id=?",
                (run_id,),
            ).fetchall()
        }
        stale = existing_keys - expected_keys
        if stale:
            placeholders = ",".join("?" for _ in stale)
            connection.execute(
                f"DELETE FROM normalized_evidence WHERE evidence_key IN ({placeholders})",
                tuple(stale),
            )
        return [
            WorkflowDatabase._upsert_normalized_evidence(connection, item)
            for item in normalized
        ]

    def _refresh_experiment_evidence(
        self,
        connection: sqlite3.Connection,
        experiment_id: str,
    ) -> None:
        run_ids = [
            row["id"] for row in connection.execute(
                "SELECT id FROM runs WHERE experiment_id=? ORDER BY id",
                (experiment_id,),
            ).fetchall()
        ]
        for run_id in run_ids:
            self._refresh_run_evidence(connection, run_id)

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

    def add_run(self, run: RunResult, source_path: str = "") -> None:
        with self.connect() as connection:
            self._upsert_run(connection, run, source_path)
            self._refresh_run_evidence(connection, run.id)

    @staticmethod
    def _upsert_run(
        connection: sqlite3.Connection,
        run: RunResult,
        source_path: str = "",
    ) -> None:
        connection.execute(
            """INSERT INTO runs(id, experiment_id, work_item_id, session_id, status, route_json,
               dashboard_url, metrics_json, raw_result_json, error_json, started_at, finished_at, source_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET session_id=excluded.session_id,
               status=excluded.status,route_json=excluded.route_json,
               dashboard_url=excluded.dashboard_url,
               metrics_json=excluded.metrics_json,
               raw_result_json=excluded.raw_result_json, error_json=excluded.error_json,
               started_at=excluded.started_at,finished_at=excluded.finished_at,
               source_path=excluded.source_path""",
            (
                run.id, run.experiment_id, run.work_item_id,
                run.session_id or None, run.status.value,
                json.dumps(_route_payload(run.route)) if run.route else None,
                run.dashboard_url,
                json.dumps(run.metrics) if run.metrics is not None else None,
                json.dumps(run.raw_result) if run.raw_result is not None else None,
                json.dumps(run.error) if run.error is not None else None,
                run.started_at, run.finished_at, source_path,
            ),
        )

    def add_failure_run_awaiting_evaluation(
        self,
        run: RunResult,
        *,
        batch_id: str,
        worker_id: str,
    ) -> None:
        """Persist terminal evidence and analysis transition atomically."""
        if not run.work_item_id:
            raise ValueError("failure run requires work_item_id")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM work_items WHERE id=?", (run.work_item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown work item: {run.work_item_id}")
            if row["state"] not in {
                WorkState.RUNNING.value, WorkState.BLOCKED.value,
            }:
                raise ValueError(
                    f"cannot analyze execution failure from {row['state']}"
                )
            self._upsert_run(connection, run)
            self._refresh_run_evidence(connection, run.id)
            connection.execute(
                """UPDATE work_items SET state='running',claimed_by=?,claimed_at=?,
                   blocker_code='awaiting_batch_evaluation',blocker_detail=?,
                   retry_after=NULL,updated_at=? WHERE id=?""",
                (worker_id, now, batch_id, now, run.work_item_id),
            )
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES('work_item',?,'execution_failure_queued_for_analysis',?,?)""",
                (
                    run.work_item_id,
                    json.dumps({
                        "from": row["state"], "to": "running",
                        "batch_id": batch_id, "run_id": run.id,
                    }, sort_keys=True),
                    now,
                ),
            )

    @staticmethod
    def _append_evaluation(
        connection: sqlite3.Connection,
        evaluation: Evaluation,
    ) -> None:
        """Append one verdict revision, superseding the evaluator's latest.

        Callers must hold an open BEGIN IMMEDIATE transaction so the
        supersede and sequence allocation stay atomic.
        """
        now = utc_now()
        superseded = connection.execute(
            """SELECT id FROM evaluation_history
               WHERE experiment_id=? AND evaluator=? AND superseded_at IS NULL""",
            (evaluation.experiment_id, evaluation.evaluator),
        ).fetchall()
        if superseded:
            placeholders = ",".join("?" for _ in superseded)
            connection.execute(
                f"""UPDATE evaluation_history SET superseded_at=?
                    WHERE id IN ({placeholders})""",
                (now, *(row["id"] for row in superseded)),
            )
        sequence = connection.execute(
            """SELECT COALESCE(MAX(sequence)+1, 0) FROM evaluation_history
               WHERE experiment_id=? AND evaluator=?""",
            (evaluation.experiment_id, evaluation.evaluator),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO evaluation_history(experiment_id, verdict, summary,
               metrics_summary, next_step, evaluator, evaluated_at, sequence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (evaluation.experiment_id, evaluation.verdict.value, evaluation.summary,
             evaluation.metrics_summary, evaluation.next_step, evaluation.evaluator,
             evaluation.evaluated_at, sequence),
        )

    def add_evaluation(self, evaluation: Evaluation) -> None:
        from .research_memory import enqueue_learning_safely

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._append_evaluation(connection, evaluation)
            self._refresh_experiment_evidence(
                connection, evaluation.experiment_id,
            )
            if connection.execute(
                "SELECT 1 FROM normalized_evidence WHERE experiment_id=? LIMIT 1",
                (evaluation.experiment_id,),
            ).fetchone():
                enqueue_learning_safely(connection, evaluation)

    def add_run_and_evaluation(self, run: RunResult, evaluation: Evaluation, source_path: str = "") -> None:
        """Persist evidence and its research verdict in one transaction."""
        from .research_memory import enqueue_learning_safely

        if run.experiment_id != evaluation.experiment_id:
            raise ValueError("run and evaluation experiment_id must match")
        with self.connect() as connection:
            self._upsert_run(connection, run, source_path)
            self._append_evaluation(connection, evaluation)
            self._refresh_run_evidence(connection, run.id)
            enqueue_learning_safely(connection, evaluation)

    def remaining_chain_count(self) -> int:
        """Count unresolved research chains, avoiding significance/baseline double-counting."""
        with self.connect() as connection:
            tracked = connection.execute(
                """SELECT COUNT(*) FROM synthesis_cohort_chains chain
                   WHERE EXISTS (
                       SELECT 1 FROM json_each(chain.work_item_ids_json) member
                       JOIN work_items w ON w.id=member.value
                       WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                         AND COALESCE(
                           json_extract(
                             w.specification_json,'$.readiness.status'
                           ),
                           'ready'
                         )!='requirements_pending'
                   )"""
            ).fetchone()[0]
            untracked = connection.execute(
                """SELECT COUNT(DISTINCT w.experiment_id) FROM work_items w
                   WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                     AND COALESCE(
                       json_extract(
                         w.specification_json,'$.readiness.status'
                       ),
                       'ready'
                     )!='requirements_pending'
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
            remaining = self._remaining_chain_count(connection)
            if remaining > low_watermark:
                return None
            recent_failures = connection.execute(
                """SELECT COUNT(*) FROM synthesis_cohorts
                   WHERE status='failed' AND updated_at>?""",
                (cooldown_start,),
            ).fetchone()[0]
            # A failed planning response is not a blocker for an underfilled
            # queue: permit one replacement attempt. Bound repeated provider
            # failures with the normal retry cooldown after two attempts.
            if int(recent_failures) >= _MAX_RECENT_SYNTHESIS_FAILURES or (
                int(recent_failures) and remaining <= 0
            ):
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
                     AND COALESCE(
                       json_extract(
                         w.specification_json,'$.readiness.status'
                       ),
                       'ready'
                     )!='requirements_pending'
               )"""
        ).fetchone()[0]
        untracked = connection.execute(
            """SELECT COUNT(DISTINCT w.experiment_id) FROM work_items w
               WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                 AND COALESCE(
                   json_extract(
                     w.specification_json,'$.readiness.status'
                   ),
                   'ready'
                 )!='requirements_pending'
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

    def _binding_cohort_p_value(
        self, connection: sqlite3.Connection, fingerprint: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT r.id, r.experiment_id,
                      json_extract(r.metrics_json, '$.p_value') AS p_value
               FROM runs r JOIN experiments e ON e.id=r.experiment_id
               WHERE e.experiment_type='significance' AND r.status='finished'
                 AND json_extract(e.specification_json, '$.entry_rule.fingerprint')=?
                 AND json_extract(r.metrics_json, '$.p_value') IS NOT NULL
               ORDER BY COALESCE(r.finished_at, r.started_at) ASC, r.id ASC LIMIT 1""",
            (fingerprint,),
        ).fetchone()

    def _release_dependents(
        self, connection: sqlite3.Connection, work_item_id: str, target: str,
        decision: str, active_limit: int, active: int, now: str,
        findings: dict | None = None,
    ) -> tuple[list[str], int]:
        changed: list[str] = []
        dependents = connection.execute(
            """SELECT id,specification_json FROM work_items
               WHERE state='scheduled' AND EXISTS (
                   SELECT 1 FROM json_each(work_items.dependencies_json)
                   WHERE value=?
               ) ORDER BY priority,created_at,id""",
            (work_item_id,),
        ).fetchall()
        for row in dependents:
            state = target
            if target == "ready" and int(active) >= active_limit:
                state = "scheduled"
            elif state == "ready":
                active += 1
            specification = json.loads(row["specification_json"])
            specification["gate_decision"] = (
                decision if state != "scheduled" else "significance_passed_capacity_held"
                if decision == "significance_passed" else decision
            )
            if findings is not None:
                specification["gate_findings"] = findings
            connection.execute(
                """UPDATE work_items SET state=?,specification_json=?,updated_at=?
                   WHERE id=? AND state='scheduled'""",
                (state, json.dumps(specification, sort_keys=True), now, row["id"]),
            )
            changed.append(row["id"])
        return changed, active

    def reconcile_significance_gate(
        self, work_item_id: str, p_value: float, active_limit: int,
        fdr_level: float = 0.05,
    ) -> dict:
        """Release or terminalize baselines dependent on completed significance work.

        First-test-wins: only the earliest finished significance run for an
        entry fingerprint may flip dependent readiness. Later tests are stored
        but reported as superseded without touching dependents. Cohort members
        are additionally gated by Benjamini-Hochberg FDR control across the
        whole cohort family once every member has a binding test.
        """
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT experiment_id,specification_json FROM work_items WHERE id=?",
                (work_item_id,),
            ).fetchone()
            fingerprint = None
            cohort_id = None
            if item is not None:
                specification = json.loads(item["specification_json"])
                entry_rule = specification.get("entry_rule")
                if isinstance(entry_rule, dict):
                    fingerprint = entry_rule.get("fingerprint")
                    cohort_id = entry_rule.get("cohort_id")
            if cohort_id:
                from .synthesis import benjamini_hochberg
                members = connection.execute(
                    """SELECT w.id AS work_item_id, w.experiment_id
                       FROM work_items w
                       WHERE json_extract(w.specification_json,'$.operation')='significance'
                         AND json_extract(w.specification_json,'$.entry_rule.cohort_id')=?
                       ORDER BY json_extract(w.specification_json,'$.entry_rule.cohort_slot') ASC, w.id ASC""",
                    (cohort_id,),
                ).fetchall()
                family: list[dict[str, Any]] = []
                for member in members:
                    member_fingerprint = None
                    member_experiment = connection.execute(
                        "SELECT specification_json FROM experiments WHERE id=?",
                        (member["experiment_id"],),
                    ).fetchone()
                    if member_experiment is not None:
                        member_specification = json.loads(
                            member_experiment["specification_json"]
                        )
                        member_entry_rule = member_specification.get("entry_rule")
                        if isinstance(member_entry_rule, dict):
                            member_fingerprint = member_entry_rule.get("fingerprint")
                    binding = (
                        self._binding_cohort_p_value(connection, member_fingerprint)
                        if member_fingerprint else None
                    )
                    family.append({
                        "work_item_id": str(member["work_item_id"]),
                        "p_value": (
                            float(binding["p_value"]) if binding is not None else None
                        ),
                    })
                if any(member["p_value"] is None for member in family):
                    return {
                        "decision": "awaiting_cohort_fdr",
                        "dependents": [],
                        "cohort_fdr": {
                            "cohort_id": cohort_id, "fdr_level": fdr_level,
                            "family_size": len(family),
                            "tested": sum(
                                member["p_value"] is not None for member in family
                            ),
                        },
                    }
                findings = benjamini_hochberg(
                    [member["p_value"] for member in family], fdr_level,
                )
                changed: list[str] = []
                active = connection.execute(
                    "SELECT COUNT(*) FROM work_items WHERE state IN ('ready','running')"
                ).fetchone()[0]
                member_findings: list[dict[str, Any]] = []
                for member, finding in zip(family, findings):
                    raw = member["p_value"]
                    if finding["rejected"] and raw < 0.05:
                        target, decision = "ready", "significance_passed_bh_fdr"
                    elif not finding["rejected"] and raw < 0.05:
                        target, decision = "scheduled", "significance_withheld_bh_fdr"
                    elif raw <= 0.10:
                        target, decision = "scheduled", "significance_inconclusive"
                    else:
                        target, decision = "archived", "significance_failed"
                    gate_findings = {
                        "procedure": "benjamini_hochberg",
                        "fdr_level": fdr_level,
                        "family_size": len(family),
                        "rank": finding["rank"],
                        "threshold": finding["threshold"],
                        "rejected": finding["rejected"],
                    }
                    released, active = self._release_dependents(
                        connection, member["work_item_id"], target, decision,
                        active_limit, active, now, findings=gate_findings,
                    )
                    changed.extend(released)
                    member_findings.append({
                        **member, **finding, "decision": decision,
                    })
                return {
                    "decision": "cohort_fdr_applied",
                    "dependents": changed,
                    "cohort_fdr": {
                        "cohort_id": cohort_id, "fdr_level": fdr_level,
                        "family_size": len(family), "members": member_findings,
                    },
                }
            binding = None
            if fingerprint:
                binding = self._binding_cohort_p_value(connection, fingerprint)
            if binding is not None and item is not None \
                    and binding["experiment_id"] != item["experiment_id"]:
                return {"decision": "superseded_by_first_test", "dependents": []}
            if binding is not None:
                p_value = float(binding["p_value"])
            if p_value < 0.05:
                target, decision = "ready", "significance_passed"
            elif p_value <= 0.10:
                target, decision = "scheduled", "significance_inconclusive"
            else:
                target, decision = "archived", "significance_failed"
            active = connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE state IN ('ready','running')"
            ).fetchone()[0]
            changed, _ = self._release_dependents(
                connection, work_item_id, target, decision, active_limit, active, now,
            )
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
            experiment = json.loads(row["experiment_json"])
            work_item = json.loads(row["specification_json"])
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