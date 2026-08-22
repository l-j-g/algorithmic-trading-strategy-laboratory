"""Runs, evaluations, and the normalized evidence store."""

from __future__ import annotations

import json
import sqlite3
from typing import Mapping

from .database_support import (
    _EVIDENCE_COLUMNS,
    _EVIDENCE_FILTERS,
    _json_object,
    _route_payload,
)
from .evidence import NormalizedEvidence, evidence_key, normalize_run_evidence
from .models import Evaluation, RunResult, WorkState, utc_now


class EvidenceMixin:
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
                outcomes = EvidenceMixin._refresh_run_evidence(
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
            EvidenceMixin._upsert_normalized_evidence(connection, item)
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
