"""SQLite storage and transactional queue operations."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from . import SCHEMA_VERSION
from .evidence import (
    NormalizedEvidence,
    evidence_key,
    normalize_run_evidence,
)
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


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("evidence JSON must be an object")
    return decoded


def _route_payload(
    route: RouteSpec | dict[str, Any],
) -> dict[str, Any]:
    return dict(route) if isinstance(route, dict) else asdict(route)


def _duration_ms(started_at: str, finished_at: str) -> int:
    def parse(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return max(0, int((parse(finished_at) - parse(started_at)).total_seconds() * 1000))


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
            run_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "raw_result_json" not in run_columns:
                connection.execute("ALTER TABLE runs ADD COLUMN raw_result_json TEXT")
            checkpoint_columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(direct_execution_sessions)"
                ).fetchall()
            }
            checkpoint_additions = {
                "first_observed_at": "TEXT",
                "last_observed_at": "TEXT",
                "last_jesse_updated_at": "TEXT",
                "last_progress": "REAL",
                "unchanged_observations": "INTEGER NOT NULL DEFAULT 0",
                "recovery_attempted": "INTEGER NOT NULL DEFAULT 0",
                "replacement_created": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in checkpoint_additions.items():
                if name not in checkpoint_columns:
                    connection.execute(
                        f"ALTER TABLE direct_execution_sessions ADD COLUMN {name} {declaration}"
                    )
            migration = connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
            if migration.rowcount:
                self._backfill_normalized_evidence(connection, record_event=False)

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

    def _backfill_normalized_evidence(
        self,
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
                outcomes = self._refresh_run_evidence(connection, run_id)
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

    def _upsert_normalized_evidence(
        self,
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

    def _refresh_run_evidence(
        self,
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
            self._upsert_normalized_evidence(connection, item)
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

    def hpo_studies(
        self,
        filters: Mapping[str, object] | None = None,
        *,
        limit: int = 100,
    ) -> list[dict]:
        allowed = {
            "id", "study_name", "strategy", "parent_experiment_id",
            "parent_work_item_id", "hpo_experiment_id", "hpo_work_item_id",
            "lifecycle_state", "direction",
        }
        filters = dict(filters or {})
        unknown = sorted(set(filters) - allowed)
        if unknown:
            raise ValueError("unknown HPO study filters: " + ", ".join(unknown))
        clauses = []
        values: list[object] = []
        for field, value in filters.items():
            if value is None:
                clauses.append(f"s.{field} IS NULL")
            else:
                clauses.append(f"s.{field}=?")
                values.append(value)
        query = """SELECT s.id AS study_id,s.study_name AS name,s.strategy,
                   s.parent_experiment_id,s.parent_work_item_id,
                   s.hpo_experiment_id,s.hpo_work_item_id,
                   s.lifecycle_state,s.objective_name,s.direction,
                   s.trial_count,s.completed_trial_count,
                   s.started_at,s.completed_at,s.updated_at,
                   (SELECT COUNT(*) FROM hpo_selected_trials x
                    WHERE x.study_id=s.id) AS selected_trial_count,
                   (SELECT COUNT(*) FROM hpo_validation_jobs v
                    WHERE v.study_id=s.id) AS validation_count,
                   d.disposition,d.finding,d.next_action,d.decided_at
                   FROM hpo_studies s
                   LEFT JOIN hpo_dispositions d ON d.study_id=s.id"""
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY s.updated_at DESC,s.id LIMIT ?"
        values.append(max(1, min(int(limit), 5000)))
        studies = self.rows(query, tuple(values))
        wants_candidates = (
            "lifecycle_state" not in filters
            or filters.get("lifecycle_state") == "hpo_candidate"
        )
        if wants_candidates:
            candidates = self.rows(
                """SELECT 'candidate:' || e.id AS study_id,
                          s.name || ' candidate' AS name,s.name AS strategy,
                          e.id AS parent_experiment_id,
                          (SELECT w.id FROM work_items w
                           WHERE w.experiment_id=e.id
                           ORDER BY w.updated_at DESC,w.id LIMIT 1
                          ) AS parent_work_item_id,
                          NULL AS hpo_experiment_id,NULL AS hpo_work_item_id,
                          'hpo_candidate' AS lifecycle_state,
                          (SELECT ne.optimizer_objective
                           FROM normalized_evidence ne
                           WHERE ne.experiment_id=e.id
                             AND ne.optimizer_objective IS NOT NULL
                           ORDER BY ne.completed_at DESC LIMIT 1
                          ) AS objective_name,
                          'maximize' AS direction,0 AS trial_count,
                          0 AS completed_trial_count,
                          NULL AS started_at,ev.evaluated_at AS completed_at,
                          ev.evaluated_at AS updated_at,
                          0 AS selected_trial_count,0 AS validation_count,
                          NULL AS disposition,ev.summary AS finding,
                          ev.next_step AS next_action,NULL AS decided_at
                   FROM evaluations ev
                   JOIN experiments e ON e.id=ev.experiment_id
                   LEFT JOIN strategies s ON s.id=e.strategy_id
                   WHERE ev.verdict='hpo_candidate'
                     AND ev.id=(
                       SELECT latest.id FROM evaluations latest
                       WHERE latest.experiment_id=e.id
                       ORDER BY latest.evaluated_at DESC,latest.id DESC LIMIT 1
                     )
                     AND NOT EXISTS (
                       SELECT 1 FROM hpo_studies hs
                       WHERE hs.parent_experiment_id=e.id
                     )"""
            )
            for candidate in candidates:
                if any(
                    candidate.get({
                        "id": "study_id",
                        "study_name": "name",
                    }.get(field, field)) != value
                    for field, value in filters.items()
                ):
                    continue
                studies.append(candidate)
        studies.sort(
            key=lambda item: item.get("updated_at") or "", reverse=True,
        )
        return studies[:max(1, min(int(limit), 5000))]

    def hpo_study_detail(self, study_id: str) -> dict | None:
        studies = self.hpo_studies({"id": study_id}, limit=1)
        if not studies:
            return None
        if study_id.startswith("candidate:"):
            studies[0].update({
                "selected_trials": [],
                "proposed_defaults": [],
                "narrowed_ranges": [],
                "validations": [],
                "analysis_job": None,
                "timings": [],
            })
            return studies[0]
        selected = self.rows(
            """SELECT x.rank,x.classification,x.selection_reason,
                      t.id AS trial_id,t.trial_number,t.objective_value,
                      t.evidence_run_id
               FROM hpo_selected_trials x
               JOIN hpo_trials t ON t.id=x.trial_id
               WHERE x.study_id=? ORDER BY
                 CASE WHEN x.rank IS NULL THEN 1 ELSE 0 END,x.rank,t.trial_number""",
            (study_id,),
        )
        for trial in selected:
            evidence_run_id = trial.pop("evidence_run_id")
            evidence = self.normalized_evidence_for_run(
                evidence_run_id,
            ) if evidence_run_id else []
            trial["evidence"] = [
                {
                    "evidence_key": evidence_key(item),
                    "run_id": item.run_id,
                    "session_id": item.session_id,
                    "evidence_split": (
                        item.evidence_split.value if item.evidence_split else None
                    ),
                }
                for item in evidence
            ]
            primary = next(
                (
                    item for item in trial["evidence"]
                    if item["evidence_split"] == "holdout"
                ),
                trial["evidence"][0] if trial["evidence"] else {},
            )
            trial["evidence_key"] = primary.get("evidence_key")
            trial["run_id"] = primary.get("run_id")
            trial["session_id"] = primary.get("session_id")
        analysis = self.rows(
            """SELECT id AS job_id,study_id,state,attempts,cohort_id,
                      claimed_by,claimed_at,retry_after,last_error,
                      created_at,updated_at,completed_at
               FROM hpo_analysis_jobs WHERE study_id=?
               ORDER BY updated_at DESC,id DESC LIMIT 1""",
            (study_id,),
        )
        proposed_defaults = self.rows(
            """SELECT parameter_name,value_json,source_trial_id,rationale
               FROM hpo_proposed_defaults WHERE study_id=?
               ORDER BY parameter_name""",
            (study_id,),
        )
        for item in proposed_defaults:
            item["value"] = json.loads(item.pop("value_json"))
        studies[0].update({
            "selected_trials": selected,
            "proposed_defaults": proposed_defaults,
            "narrowed_ranges": self.rows(
                """SELECT parameter_name,low_value,high_value,step_value,
                          logarithmic
                   FROM hpo_narrowed_ranges WHERE study_id=?
                   ORDER BY parameter_name""",
                (study_id,),
            ),
            "validations": self.rows(
                """SELECT v.*,w.state AS work_state,w.blocker_code,
                          w.blocker_detail,
                          json_extract(
                            w.specification_json,'$.readiness.status'
                          ) AS readiness_status
                   FROM hpo_validation_jobs v
                   JOIN work_items w ON w.id=v.work_item_id
                   WHERE v.study_id=? ORDER BY v.created_at,v.id""",
                (study_id,),
            ),
            "analysis_job": analysis[0] if analysis else None,
            "timings": self.work_item_stage_timings(
                studies[0]["hpo_work_item_id"], limit=100,
            ) if studies[0]["hpo_work_item_id"] else [],
        })
        return studies[0]

    def diagnostic_hpo_trial_details(
        self, study_id: str, trial_number: int,
    ) -> dict | None:
        rows = self.rows(
            """SELECT * FROM hpo_trials
               WHERE study_id=? AND trial_number=?""",
            (study_id, trial_number),
        )
        if not rows:
            return None
        result = rows[0]
        for field in ("params_json", "user_attrs_json", "system_attrs_json"):
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    def current_analyzer_status(self) -> dict | None:
        rows = self.rows(
            """SELECT j.id AS job_id,j.study_id,j.state,j.attempts,
                      j.cohort_id,j.claimed_by,j.claimed_at,j.retry_after,
                      j.last_error,j.updated_at,
                      s.study_name AS name,s.strategy,s.lifecycle_state
               FROM hpo_analysis_jobs j
               JOIN hpo_studies s ON s.id=j.study_id
               ORDER BY j.updated_at DESC,j.id DESC LIMIT 1"""
        )
        return rows[0] if rows else None

    def hpo_study_for_work_item(self, work_item_id: str) -> dict | None:
        rows = self.hpo_studies({"hpo_work_item_id": work_item_id}, limit=1)
        return rows[0] if rows else None

    def hpo_analysis_payload(
        self,
        study_id: str,
        *,
        limit: int = 50,
    ) -> dict | None:
        """Return HPO metadata plus canonical trial evidence; never parameters."""
        studies = self.hpo_studies({"id": study_id}, limit=1)
        if not studies:
            return None
        direction = studies[0]["direction"]
        order = "DESC" if direction == "maximize" else "ASC"
        trials = self.rows(
            f"""SELECT trial_number,objective_value,evidence_run_id,
                       state,started_at,completed_at
                FROM hpo_trials WHERE study_id=? AND state='COMPLETE'
                ORDER BY CASE WHEN objective_value IS NULL THEN 1 ELSE 0 END,
                         objective_value {order},trial_number
                LIMIT ?""",
            (study_id, max(1, min(int(limit), 1000))),
        )
        for trial in trials:
            evidence = self.normalized_evidence_for_run(
                trial.pop("evidence_run_id"),
            )
            trial["evidence"] = [item.to_dict() for item in evidence]
        return {"study": studies[0], "trials": trials}

    def select_hpo_trials(
        self,
        study_id: str,
        selections: list[Mapping[str, Any]],
    ) -> list[dict]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM hpo_studies WHERE id=?", (study_id,),
            ).fetchone() is None:
                raise KeyError(f"unknown HPO study: {study_id}")
            for selection in selections:
                number = int(selection["trial_number"])
                trial = connection.execute(
                    """SELECT id FROM hpo_trials
                       WHERE study_id=? AND trial_number=?""",
                    (study_id, number),
                ).fetchone()
                if trial is None:
                    raise KeyError(f"unknown HPO trial: {study_id}/{number}")
                classification = str(
                    selection.get("classification") or "validation_candidate"
                )
                if classification not in {
                    "likely_overfit", "validation_candidate",
                    "selected", "not_selected",
                }:
                    raise ValueError(
                        f"invalid HPO trial classification: {classification}"
                    )
                connection.execute(
                    """INSERT INTO hpo_selected_trials(
                           study_id,trial_id,rank,classification,
                           selection_reason,selected_at
                       ) VALUES (?,?,?,?,?,?)
                       ON CONFLICT(study_id,trial_id) DO UPDATE SET
                           rank=excluded.rank,
                           classification=excluded.classification,
                           selection_reason=excluded.selection_reason,
                           selected_at=excluded.selected_at""",
                    (
                        study_id, trial["id"], selection.get("rank"),
                        classification,
                        str(selection.get("selection_reason") or ""), now,
                    ),
                )
        detail = self.hpo_study_detail(study_id)
        return detail["selected_trials"] if detail else []

    def start_hpo_study(
        self,
        study_id: str,
        *,
        started_at: str | None = None,
    ) -> dict:
        now = started_at or utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE hpo_studies SET lifecycle_state='hpo_running',
                   started_at=COALESCE(started_at,?),updated_at=?
                   WHERE id=? AND lifecycle_state IN (
                     'hpo_candidate','hpo_scheduled'
                   )""",
                (now, now, study_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"HPO study cannot start: {study_id}")
            return dict(connection.execute(
                "SELECT * FROM hpo_studies WHERE id=?", (study_id,),
            ).fetchone())

    def schedule_hpo_validations(
        self,
        study_id: str,
        trial_numbers: list[int],
        *,
        evidence_splits: tuple[str, ...] = ("oos",),
    ) -> list[dict]:
        """Schedule validation jobs by trial reference; parameters stay diagnostic."""
        if not trial_numbers:
            raise ValueError("at least one trial is required")
        if not evidence_splits or any(
            split not in {"train", "holdout", "oos", "rolling"}
            for split in evidence_splits
        ):
            raise ValueError("invalid evidence split")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            study = connection.execute(
                """SELECT s.*,e.strategy_id,e.hypothesis,e.archetype,
                          e.target_regime,e.failure_regime,e.specification_json
                   FROM hpo_studies s JOIN experiments e
                     ON e.id=s.hpo_experiment_id WHERE s.id=?""",
                (study_id,),
            ).fetchone()
            if study is None:
                raise KeyError(f"unknown HPO study: {study_id}")
            created = []
            for number in trial_numbers:
                trial = connection.execute(
                    """SELECT t.id FROM hpo_trials t
                       JOIN hpo_selected_trials x ON x.trial_id=t.id
                       WHERE t.study_id=? AND t.trial_number=?
                         AND x.classification IN (
                           'validation_candidate','selected'
                         )""",
                    (study_id, int(number)),
                ).fetchone()
                if trial is None:
                    raise ValueError(
                        f"trial is not selected for validation: {number}"
                    )
                for split in evidence_splits:
                    stable = hashlib.sha256(
                        f"{study_id}:{number}:{split}".encode()
                    ).hexdigest()[:12].upper()
                    experiment_id = f"HPO-VAL-{stable}"
                    work_item_id = f"{experiment_id}-JOB"
                    validation_id = f"{experiment_id}-{split}"
                    specification = _json_object(study["specification_json"])
                    specification.update({
                        "id": experiment_id,
                        "experiment_type": "out_of_sample",
                        "parent_experiment_id": study["hpo_experiment_id"],
                    })
                    routes = specification.get("routes")
                    has_routes = (
                        isinstance(routes, list) and bool(routes)
                    )
                    work_specification = {
                        "operation": "backtest",
                        "hpo_study_id": study_id,
                        "hpo_trial_id": trial["id"],
                        "evidence_split": split,
                        "readiness": {
                            "status": (
                                "ready"
                                if has_routes
                                else "requirements_pending"
                            ),
                            "missing": (
                                []
                                if has_routes
                                else ["validation_routes"]
                            ),
                        },
                    }
                    connection.execute(
                        """INSERT OR IGNORE INTO experiments(
                               id,strategy_id,experiment_type,hypothesis,archetype,
                               target_regime,failure_regime,specification_json,
                               parent_experiment_id,source_path,created_at,updated_at
                           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            experiment_id, study["strategy_id"], "out_of_sample",
                            study["hypothesis"], study["archetype"],
                            study["target_regime"], study["failure_regime"],
                            json.dumps(specification, sort_keys=True),
                            study["hpo_experiment_id"], "hpo-validation",
                            now, now,
                        ),
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO work_items(
                               id,experiment_id,priority,state,dependencies_json,
                               specification_json,created_at,updated_at
                           ) VALUES (?,?,40,'scheduled',?,?,?,?)""",
                        (
                            work_item_id, experiment_id,
                            json.dumps([study["hpo_work_item_id"]]),
                            json.dumps(work_specification, sort_keys=True),
                            now, now,
                        ),
                    )
                    connection.execute(
                        """UPDATE work_items SET specification_json=?,
                           blocker_code=?,blocker_detail=?,updated_at=?
                           WHERE id=? AND state='scheduled'""",
                        (
                            json.dumps(work_specification, sort_keys=True),
                            None if has_routes else "requirements_pending",
                            (
                                None
                                if has_routes
                                else (
                                    "Canonical symbol/timeframe/OOS or rolling "
                                    "validation periods are required."
                                )
                            ),
                            now, work_item_id,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO hpo_validation_jobs(
                               id,study_id,trial_id,experiment_id,work_item_id,
                               evidence_split,state,created_at
                           ) VALUES (?,?,?,?,?,?,'scheduled',?)
                           ON CONFLICT(study_id,trial_id,evidence_split)
                           DO UPDATE SET experiment_id=excluded.experiment_id,
                               work_item_id=excluded.work_item_id""",
                        (
                            validation_id, study_id, trial["id"],
                            experiment_id, work_item_id, split, now,
                        ),
                    )
                    created.append(validation_id)
            connection.execute(
                """UPDATE hpo_studies SET lifecycle_state='validation',
                   updated_at=? WHERE id=?""",
                (now, study_id),
            )
            connection.execute(
                """UPDATE hpo_analysis_jobs SET state='completed',
                   completed_at=?,updated_at=?,claimed_by=NULL,claimed_at=NULL
                   WHERE study_id=? AND state='running'""",
                (now, now, study_id),
            )
            placeholders = ",".join("?" for _ in created)
            return [
                dict(row) for row in connection.execute(
                    f"""SELECT * FROM hpo_validation_jobs
                        WHERE id IN ({placeholders}) ORDER BY id""",
                    created,
                ).fetchall()
            ]

    def configure_hpo_validation_routes(
        self,
        study_id: str,
        routes_by_split: Mapping[str, object],
        *,
        updated_by: str = "operator",
    ) -> dict:
        """Attach validated split-specific routes and release pending jobs."""
        normalized: dict[str, list[dict[str, str]]] = {}
        for split, raw_routes in routes_by_split.items():
            if split not in {"oos", "rolling"}:
                raise ValueError(f"unsupported validation split: {split}")
            if not isinstance(raw_routes, list) or not raw_routes:
                raise ValueError(
                    f"validation routes must be a non-empty list: {split}"
                )
            routes = []
            for raw in raw_routes:
                if not isinstance(raw, dict):
                    raise ValueError(
                        f"validation route must be an object: {split}"
                    )
                try:
                    route = RouteSpec(**raw)
                except TypeError as error:
                    raise ValueError(
                        f"invalid validation route for {split}: {error}"
                    ) from error
                if any(
                    not getattr(route, field).strip()
                    for field in (
                        "exchange", "symbol", "timeframe",
                        "start_date", "finish_date",
                    )
                ):
                    raise ValueError(
                        f"validation route fields must be non-empty: {split}"
                    )
                routes.append(asdict(route))
            normalized[split] = routes
        if not normalized:
            raise ValueError("at least one validation split is required")
        now = utc_now()
        updated = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM hpo_studies WHERE id=?", (study_id,),
            ).fetchone() is None:
                raise KeyError(f"unknown HPO study: {study_id}")
            for split, routes in normalized.items():
                jobs = connection.execute(
                    """SELECT v.id,v.experiment_id,v.work_item_id,
                              e.specification_json AS experiment_json,
                              w.specification_json AS work_json
                       FROM hpo_validation_jobs v
                       JOIN experiments e ON e.id=v.experiment_id
                       JOIN work_items w ON w.id=v.work_item_id
                       WHERE v.study_id=? AND v.evidence_split=?""",
                    (study_id, split),
                ).fetchall()
                if not jobs:
                    raise ValueError(
                        f"study has no validation jobs for split: {split}"
                    )
                for job in jobs:
                    experiment = _json_object(job["experiment_json"])
                    experiment["routes"] = routes
                    work = _json_object(job["work_json"])
                    work["readiness"] = {
                        "status": "ready", "missing": [],
                    }
                    connection.execute(
                        """UPDATE experiments SET specification_json=?,
                           updated_at=? WHERE id=?""",
                        (
                            json.dumps(experiment, sort_keys=True),
                            now, job["experiment_id"],
                        ),
                    )
                    connection.execute(
                        """UPDATE work_items SET specification_json=?,
                           state=CASE
                             WHEN state='blocked'
                              AND blocker_code='requirements_pending'
                             THEN 'scheduled' ELSE state END,
                           blocker_code=NULL,blocker_detail=NULL,updated_at=?
                           WHERE id=?""",
                        (
                            json.dumps(work, sort_keys=True),
                            now, job["work_item_id"],
                        ),
                    )
                    updated.append(job["work_item_id"])
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,
                       occurred_at
                   ) VALUES ('hpo_study',?,'validation_routes_configured',?,?)""",
                (
                    study_id,
                    json.dumps({
                        "updated_by": updated_by,
                        "splits": {
                            split: len(routes)
                            for split, routes in normalized.items()
                        },
                        "work_item_ids": sorted(updated),
                    }, sort_keys=True),
                    now,
                ),
            )
        return {
            "study_id": study_id,
            "updated_work_items": sorted(updated),
            "splits": {
                split: len(routes)
                for split, routes in normalized.items()
            },
        }

    def schedule_hpo_candidate(
        self,
        parent_experiment_id: str,
        parent_work_item_id: str,
        *,
        study_name: str | None = None,
        objective_name: str = "objective",
        direction: str = "maximize",
    ) -> dict:
        """Atomically schedule HPO from durable hpo_candidate evidence."""
        if direction not in {"maximize", "minimize"}:
            raise ValueError("direction must be maximize or minimize")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """SELECT * FROM hpo_studies
                   WHERE parent_experiment_id=? AND parent_work_item_id=?
                     AND source_database_path IS NULL
                   ORDER BY created_at DESC LIMIT 1""",
                (parent_experiment_id, parent_work_item_id),
            ).fetchone()
            if existing is not None:
                return dict(existing)
            parent = connection.execute(
                """SELECT e.*,s.name AS strategy,w.id AS parent_work_item_id,
                          (SELECT verdict FROM evaluations ev
                           WHERE ev.experiment_id=e.id
                           ORDER BY ev.evaluated_at DESC,ev.id DESC LIMIT 1) AS verdict
                   FROM experiments e
                   JOIN strategies s ON s.id=e.strategy_id
                   JOIN work_items w ON w.id=? AND w.experiment_id=e.id
                   WHERE e.id=?""",
                (parent_work_item_id, parent_experiment_id),
            ).fetchone()
            if parent is None:
                raise KeyError("unknown parent experiment/work item")
            if parent["verdict"] != "hpo_candidate":
                raise ValueError("parent verdict must be hpo_candidate")
            suffix = uuid.uuid4().hex[:12].upper()
            study_id = f"HPO-{suffix}"
            experiment_id = f"{parent_experiment_id}-HPO-{suffix}"
            work_item_id = f"{experiment_id}-JOB"
            specification = _json_object(parent["specification_json"])
            specification.update({
                "id": experiment_id,
                "experiment_type": "hpo",
                "parent_experiment_id": parent_experiment_id,
            })
            connection.execute(
                """INSERT INTO experiments(
                       id,strategy_id,experiment_type,hypothesis,archetype,
                       target_regime,failure_regime,specification_json,
                       parent_experiment_id,source_path,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    experiment_id, parent["strategy_id"], "hpo",
                    parent["hypothesis"], parent["archetype"],
                    parent["target_regime"], parent["failure_regime"],
                    json.dumps(specification, sort_keys=True),
                    parent_experiment_id, "hpo-scheduler", now, now,
                ),
            )
            connection.execute(
                """INSERT INTO work_items(
                       id,experiment_id,priority,state,dependencies_json,
                       specification_json,created_at,updated_at
                   ) VALUES (?,?,?,'scheduled',?,?,?,?)""",
                (
                    work_item_id, experiment_id, 50,
                    json.dumps([parent_work_item_id]),
                    json.dumps({
                        "operation": "hpo",
                        "hpo_study_id": study_id,
                        "optimizer_objective": objective_name,
                    }, sort_keys=True),
                    now, now,
                ),
            )
            connection.execute(
                """INSERT INTO hpo_studies(
                   id,study_name,strategy,parent_experiment_id,
                       parent_work_item_id,hpo_experiment_id,hpo_work_item_id,
                       lifecycle_state,objective_name,direction,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,'hpo_scheduled',?,?,?,?)""",
                (
                    study_id, study_name or f"{parent['strategy']}-{study_id}",
                    parent["strategy"], parent_experiment_id,
                    parent_work_item_id, experiment_id, work_item_id,
                    objective_name, direction, now, now,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM hpo_studies WHERE id=?", (study_id,),
            ).fetchone())

    def complete_hpo_study(
        self,
        study_id: str,
        *,
        completed_at: str | None = None,
    ) -> dict:
        """Move a finished study into durable analyzer queue atomically."""
        now = completed_at or utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            study = connection.execute(
                "SELECT * FROM hpo_studies WHERE id=?", (study_id,),
            ).fetchone()
            if study is None:
                raise KeyError(f"unknown HPO study: {study_id}")
            counts = connection.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN state='COMPLETE' THEN 1 ELSE 0 END) AS complete
                   FROM hpo_trials WHERE study_id=?""",
                (study_id,),
            ).fetchone()
            connection.execute(
                """UPDATE hpo_studies SET lifecycle_state='hpo_analysis',
                   trial_count=?,completed_trial_count=?,completed_at=?,
                   updated_at=? WHERE id=?""",
                (
                    counts["total"], counts["complete"] or 0,
                    now, now, study_id,
                ),
            )
            job = connection.execute(
                """SELECT * FROM hpo_analysis_jobs
                   WHERE study_id=? AND state IN (
                     'pending','running','waiting_retry','abandoned'
                   ) ORDER BY created_at DESC LIMIT 1""",
                (study_id,),
            ).fetchone()
            if job is None:
                job_id = f"HPO-ANALYSIS-{uuid.uuid4().hex[:12].upper()}"
                connection.execute(
                    """INSERT INTO hpo_analysis_jobs(
                           id,study_id,state,created_at,updated_at
                       ) VALUES (?,?,'pending',?,?)""",
                    (job_id, study_id, now, now),
                )
                job = connection.execute(
                    "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job_id,),
                ).fetchone()
            return dict(job)

    def claim_hpo_analysis(
        self,
        worker_id: str,
        *,
        cohort_id: str | None = None,
    ) -> dict | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM hpo_analysis_jobs
                   WHERE state='pending' OR (
                     state='waiting_retry' AND retry_after<=?
                   ) ORDER BY created_at,id LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE hpo_analysis_jobs SET state='running',
                   attempts=attempts+1,cohort_id=?,claimed_by=?,claimed_at=?,
                   retry_after=NULL,updated_at=? WHERE id=?""",
                (cohort_id, worker_id, now, now, row["id"]),
            )
            return dict(connection.execute(
                "SELECT * FROM hpo_analysis_jobs WHERE id=?", (row["id"],),
            ).fetchone())

    def retry_hpo_analysis(
        self,
        job_id: str,
        *,
        error: str,
        retry_after: str,
        max_attempts: int = 5,
    ) -> dict:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown HPO analysis job: {job_id}")
            state = "terminal" if row["attempts"] >= max_attempts else "waiting_retry"
            connection.execute(
                """UPDATE hpo_analysis_jobs SET state=?,last_error=?,
                   retry_after=?,claimed_by=NULL,claimed_at=NULL,updated_at=?
                   WHERE id=?""",
                (
                    state, error, None if state == "terminal" else retry_after,
                    utc_now(), job_id,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job_id,),
            ).fetchone())

    def requeue_terminal_hpo_analysis(
        self,
        job_id: str,
        *,
        reason: str,
        updated_by: str = "operator",
    ) -> dict:
        """Reopen one terminal analyzer job after its external blocker is fixed."""
        reason = reason.strip()
        if not reason:
            raise ValueError("requeue reason is required")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown HPO analysis job: {job_id}")
            if row["state"] != "terminal":
                raise ValueError(
                    f"HPO analysis is not terminal: {job_id}"
                )
            connection.execute(
                """UPDATE hpo_analysis_jobs SET state='pending',attempts=0,
                   last_error=NULL,retry_after=NULL,claimed_by=NULL,
                   claimed_at=NULL,completed_at=NULL,updated_at=?
                   WHERE id=?""",
                (now, job_id),
            )
            connection.execute(
                """UPDATE hpo_studies SET lifecycle_state='hpo_analysis',
                   updated_at=? WHERE id=?""",
                (now, row["study_id"]),
            )
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,
                       occurred_at
                   ) VALUES ('hpo_analysis',?,'hpo_analysis_requeued',?,?)""",
                (
                    job_id,
                    json.dumps({
                        "reason": reason,
                        "updated_by": updated_by,
                        "previous_attempts": row["attempts"],
                        "previous_error": row["last_error"],
                    }, sort_keys=True),
                    now,
                ),
            )
            return dict(connection.execute(
                "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job_id,),
            ).fetchone())

    def abandon_hpo_analysis(self, job_id: str, *, error: str) -> dict:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE hpo_analysis_jobs SET state='abandoned',
                   last_error=?,claimed_by=NULL,claimed_at=NULL,updated_at=?
                   WHERE id=? AND state='running'""",
                (error, utc_now(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"HPO analysis is not running: {job_id}")
            return dict(connection.execute(
                "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job_id,),
            ).fetchone())

    def recover_abandoned_hpo_analysis(
        self, claimed_before: str,
    ) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM hpo_analysis_jobs
                   WHERE (state='running' AND claimed_at<?) OR state='abandoned'
                   ORDER BY claimed_at,id""",
                (claimed_before,),
            ).fetchall()
            if rows:
                ids = [row["id"] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                connection.execute(
                    f"""UPDATE hpo_analysis_jobs SET state='pending',
                        claimed_by=NULL,claimed_at=NULL,retry_after=NULL,
                        updated_at=? WHERE id IN ({placeholders})""",
                    (utc_now(), *ids),
                )
            return [dict(row) for row in rows]

    def terminalize_hpo_analysis(
        self,
        job_id: str,
        *,
        disposition: str,
        finding: str,
        next_action: str,
    ) -> dict:
        if disposition not in {"paper_trade_candidate", "revise", "reject"}:
            raise ValueError("invalid HPO disposition")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job_id,),
            ).fetchone()
            if job is None:
                raise KeyError(f"unknown HPO analysis job: {job_id}")
            connection.execute(
                """INSERT INTO hpo_dispositions(
                       study_id,disposition,finding,next_action,decided_at
                   ) VALUES (?,?,?,?,?)
                   ON CONFLICT(study_id) DO UPDATE SET
                       disposition=excluded.disposition,
                       finding=excluded.finding,next_action=excluded.next_action,
                       decided_at=excluded.decided_at""",
                (job["study_id"], disposition, finding, next_action, now),
            )
            connection.execute(
                """UPDATE hpo_analysis_jobs SET state='completed',
                   completed_at=?,updated_at=?,claimed_by=NULL,claimed_at=NULL
                   WHERE id=?""",
                (now, now, job_id),
            )
            connection.execute(
                """UPDATE hpo_studies SET lifecycle_state=?,updated_at=?
                   WHERE id=?""",
                (disposition, now, job["study_id"]),
            )
            result = dict(connection.execute(
                """SELECT s.*,d.disposition,d.finding,d.next_action,d.decided_at
                   FROM hpo_studies s JOIN hpo_dispositions d ON d.study_id=s.id
                   WHERE s.id=?""",
                (job["study_id"],),
            ).fetchone())
            return result

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

    def add_evaluation(self, evaluation: Evaluation) -> None:
        from .research_memory import enqueue_learning_safely

        with self.connect() as connection:
            connection.execute("DELETE FROM evaluations WHERE experiment_id = ? AND evaluator = ?", (evaluation.experiment_id, evaluation.evaluator))
            connection.execute(
                """INSERT INTO evaluations(experiment_id, verdict, summary, metrics_summary,
                   next_step, evaluator, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (evaluation.experiment_id, evaluation.verdict.value, evaluation.summary,
                 evaluation.metrics_summary, evaluation.next_step, evaluation.evaluator,
                 evaluation.evaluated_at),
            )
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
                (run.id, run.experiment_id, run.work_item_id, run.session_id or None, run.status.value,
                 json.dumps(_route_payload(run.route)) if run.route else None, run.dashboard_url,
                 json.dumps(run.metrics) if run.metrics is not None else None,
                 json.dumps(run.raw_result) if run.raw_result is not None else None,
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
            self._refresh_run_evidence(connection, run.id)
            enqueue_learning_safely(connection, evaluation)

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
                   ORDER BY priority,created_at,id LIMIT ?""",
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
                     AND r.id=(
                         SELECT latest.id FROM runs latest
                         WHERE latest.work_item_id=w.id
                         ORDER BY COALESCE(latest.finished_at,'' ) DESC,
                                  latest.rowid DESC LIMIT 1
                     )"""
        parameters: tuple = ()
        if worker_id:
            query += " AND w.claimed_by=?"
            parameters = (worker_id,)
        query += " ORDER BY w.blocker_detail,w.priority,w.created_at,w.id"
        return self.rows(query, parameters)

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

    def finalize_batch_evaluation(self, evaluation: Evaluation) -> dict:
        """Persist one evaluation and finish its awaiting work item atomically."""
        from .research_memory import enqueue_learning_safely

        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM work_items WHERE experiment_id=? AND state='running'
                   AND blocker_code='awaiting_batch_evaluation' ORDER BY updated_at DESC LIMIT 1""",
                (evaluation.experiment_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"no awaiting execution for {evaluation.experiment_id}")
            connection.execute(
                "DELETE FROM evaluations WHERE experiment_id=? AND evaluator=?",
                (evaluation.experiment_id, evaluation.evaluator),
            )
            connection.execute(
                """INSERT INTO evaluations(experiment_id,verdict,summary,metrics_summary,
                   next_step,evaluator,evaluated_at) VALUES (?,?,?,?,?,?,?)""",
                (evaluation.experiment_id, evaluation.verdict.value, evaluation.summary,
                 evaluation.metrics_summary, evaluation.next_step, evaluation.evaluator,
                 evaluation.evaluated_at),
            )
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
                         SELECT 1 FROM runs WHERE runs.work_item_id=work_items.id
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
