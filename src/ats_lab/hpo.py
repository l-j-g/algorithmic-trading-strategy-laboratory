"""Read-only optimizer imports into durable ATS Lab HPO state."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .database import WorkflowDatabase
from .models import utc_now


@dataclass(frozen=True)
class OptunaTrial:
    number: int
    state: str
    objective_value: float | None
    started_at: str | None
    completed_at: str | None
    params: dict[str, Any]
    distributions: dict[str, dict[str, Any]]
    user_attrs: dict[str, Any]
    system_attrs: dict[str, Any]


@dataclass(frozen=True)
class OptunaStudy:
    source_study_id: int
    name: str
    direction: str
    trials: tuple[OptunaTrial, ...]


@dataclass(frozen=True)
class JesseSessionExport:
    """Complete, versioned Jesse optimization-session export."""

    session_id: str
    study: OptunaStudy


EMA_V7_CLASSIFICATIONS = {
    66: {
        "classification": "likely_overfit",
        "rank": None,
        "reason": (
            "Likely overfit: training Sharpe 1.914 and +25.50% net collapsed "
            "to holdout Sharpe 0.109 and +0.86% net."
        ),
    },
    332: {
        "classification": "validation_candidate",
        "rank": 1,
        "reason": (
            "Validation candidate: balanced train/holdout profitability, "
            "holdout Sharpe 1.218, and 2.17% holdout drawdown."
        ),
    },
    207: {
        "classification": "validation_candidate",
        "rank": 2,
        "reason": (
            "Validation candidate: train/holdout net profit remained close, "
            "with holdout Sharpe 1.157."
        ),
    },
    394: {
        "classification": "validation_candidate",
        "rank": 3,
        "reason": (
            "Validation candidate: positive holdout return but weaker Sharpe "
            "and wider train/holdout stability gap than trials 332 and 207."
        ),
    },
}

_OPTUNA_STATES = frozenset({"RUNNING", "COMPLETE", "PRUNED", "FAIL", "WAITING"})


def read_jesse_session_export(source_path: Path) -> JesseSessionExport:
    """Validate one complete Jesse optimization-session JSON export.

    Jesse's dashboard ``best_candidates`` response is deliberately
    insufficient. Import requires an explicit full ``trials`` array plus
    matching terminal counts, so ranked candidates cannot be mistaken for
    complete optimizer evidence.
    """
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    try:
        raw = json.loads(source_path.read_text())
    except json.JSONDecodeError as error:
        raise ValueError("invalid Jesse session export JSON") from error
    if not isinstance(raw, dict):
        raise ValueError("Jesse session export must contain an object")
    if raw.get("schema_version") != 1:
        raise ValueError("unsupported Jesse session export schema_version; expected 1")
    if raw.get("source") != "jesse_optimization_session":
        raise ValueError(
            "Jesse session export source must be 'jesse_optimization_session'"
        )
    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Jesse session export requires non-empty session_id")
    study_name = raw.get("study_name")
    if not isinstance(study_name, str) or not study_name.strip():
        raise ValueError("Jesse session export requires non-empty study_name")
    direction = raw.get("direction")
    if direction not in {"maximize", "minimize"}:
        raise ValueError("Jesse session direction must be maximize or minimize")
    if raw.get("status") != "completed":
        raise ValueError("Jesse session export must have status='completed'")
    if raw.get("trial_records_complete") is not True:
        if raw.get("best_candidates") is not None:
            raise ValueError(
                "Jesse best_candidates is partial; full trials export required"
            )
        raise ValueError(
            "Jesse session export requires trial_records_complete=true"
        )
    total_trials = _nonnegative_int(raw.get("total_trials"), "total_trials")
    completed_trials = _nonnegative_int(
        raw.get("completed_trials"), "completed_trials",
    )
    raw_trials = raw.get("trials")
    if not isinstance(raw_trials, list):
        raise ValueError("Jesse session export requires full trials array")
    if not raw_trials:
        raise ValueError("Jesse session export contains no trial records")
    if total_trials != completed_trials or completed_trials != len(raw_trials):
        raise ValueError(
            "Jesse session trial counts must match full trials array: "
            f"total_trials={total_trials}, completed_trials={completed_trials}, "
            f"records={len(raw_trials)}"
        )
    trials: list[OptunaTrial] = []
    numbers: set[int] = set()
    for index, item in enumerate(raw_trials):
        if not isinstance(item, dict):
            raise ValueError(f"Jesse trial record {index} must be an object")
        number = _nonnegative_int(item.get("number"), f"trials[{index}].number")
        if number in numbers:
            raise ValueError(f"duplicate Jesse trial number: {number}")
        numbers.add(number)
        if str(item.get("state") or "").upper() != "COMPLETE":
            raise ValueError(
                f"Jesse trial {number} must have state='COMPLETE'"
            )
        objective = _finite(item.get("objective_value"))
        if objective is None:
            raise ValueError(
                f"Jesse trial {number} requires finite objective_value"
            )
        params = item.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"Jesse trial {number} params must be an object")
        training_metrics = item.get("training_metrics")
        testing_metrics = item.get("testing_metrics")
        if not isinstance(training_metrics, dict) or not training_metrics:
            raise ValueError(
                f"Jesse trial {number} requires training_metrics object"
            )
        if not isinstance(testing_metrics, dict) or not testing_metrics:
            raise ValueError(
                f"Jesse trial {number} requires testing_metrics object"
            )
        user_attrs = item.get("user_attrs", {})
        system_attrs = item.get("system_attrs", {})
        if not isinstance(user_attrs, dict):
            raise ValueError(f"Jesse trial {number} user_attrs must be an object")
        if not isinstance(system_attrs, dict):
            raise ValueError(f"Jesse trial {number} system_attrs must be an object")
        trials.append(OptunaTrial(
            number=number,
            state="COMPLETE",
            objective_value=objective,
            started_at=_timestamp(item.get("started_at")),
            completed_at=_timestamp(item.get("completed_at")),
            params=dict(params),
            distributions={},
            user_attrs={
                **user_attrs,
                "training_metrics": dict(training_metrics),
                "testing_metrics": dict(testing_metrics),
            },
            system_attrs={
                **system_attrs,
                "source_provider": "jesse",
                "source_session_id": session_id,
            },
        ))
    trials.sort(key=lambda trial: trial.number)
    return JesseSessionExport(
        session_id=session_id,
        study=OptunaStudy(
            source_study_id=0,
            name=study_name,
            direction=direction,
            trials=tuple(trials),
        ),
    )


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Jesse session {field} must be a non-negative integer")
    return value


def read_optuna_study(
    source_path: Path,
    *,
    study_name: str,
) -> OptunaStudy:
    """Read one Optuna RDB study without importing Optuna."""
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    uri = f"{source_path.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        _validate_optuna_schema(connection)
        study = connection.execute(
            "SELECT study_id,study_name FROM studies WHERE study_name=?",
            (study_name,),
        ).fetchone()
        if study is None:
            raise KeyError(f"unknown Optuna study: {study_name}")
        direction_row = connection.execute(
            """SELECT direction FROM study_directions
               WHERE study_id=? ORDER BY objective LIMIT 1""",
            (study["study_id"],),
        ).fetchone()
        direction = (
            str(direction_row["direction"]).lower()
            if direction_row else "maximize"
        )
        if direction not in {"maximize", "minimize"}:
            raise ValueError(f"unsupported Optuna direction: {direction}")
        trials = []
        for row in connection.execute(
            """SELECT t.trial_id,t.number,t.state,t.datetime_start,
                      t.datetime_complete,
                      (SELECT value FROM trial_values v
                       WHERE v.trial_id=t.trial_id AND v.objective=0) AS objective
               FROM trials t WHERE t.study_id=?
               ORDER BY t.number""",
            (study["study_id"],),
        ):
            params: dict[str, Any] = {}
            distributions: dict[str, dict[str, Any]] = {}
            for parameter in connection.execute(
                """SELECT param_name,param_value,distribution_json
                   FROM trial_params WHERE trial_id=? ORDER BY param_name""",
                (row["trial_id"],),
            ):
                distribution = _decode_json(parameter["distribution_json"])
                distributions[parameter["param_name"]] = distribution
                params[parameter["param_name"]] = _decode_parameter(
                    parameter["param_value"], distribution,
                )
            state = str(row["state"]).upper()
            if state not in _OPTUNA_STATES:
                raise ValueError(f"unsupported Optuna trial state: {state}")
            trials.append(OptunaTrial(
                number=int(row["number"]),
                state=state,
                objective_value=_finite(row["objective"]),
                started_at=_timestamp(row["datetime_start"]),
                completed_at=_timestamp(row["datetime_complete"]),
                params=params,
                distributions=distributions,
                user_attrs=_attributes(
                    connection, "trial_user_attributes", row["trial_id"],
                ),
                system_attrs=_attributes(
                    connection, "trial_system_attributes", row["trial_id"],
                ),
            ))
        return OptunaStudy(
            source_study_id=int(study["study_id"]),
            name=str(study["study_name"]),
            direction=direction,
            trials=tuple(trials),
        )
    finally:
        connection.close()


def _validate_optuna_schema(connection: sqlite3.Connection) -> None:
    """Reject non-Optuna or partially copied SQLite files before importing."""
    required = {
        "studies": {"study_id", "study_name"},
        "study_directions": {"study_id", "direction", "objective"},
        "trials": {
            "trial_id", "number", "study_id", "state",
            "datetime_start", "datetime_complete",
        },
        "trial_values": {"trial_id", "objective", "value"},
        "trial_params": {
            "trial_id", "param_name", "param_value", "distribution_json",
        },
        "trial_user_attributes": {"trial_id", "key", "value_json"},
        "trial_system_attributes": {"trial_id", "key", "value_json"},
    }
    for table, columns in required.items():
        try:
            actual = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
        except sqlite3.DatabaseError as error:
            raise ValueError(f"invalid Optuna database table: {table}") from error
        if not actual:
            raise ValueError(f"invalid Optuna database: missing table {table}")
        missing = sorted(columns - actual)
        if missing:
            raise ValueError(
                f"invalid Optuna database: {table} missing {', '.join(missing)}"
            )


def import_optuna_study(
    database: WorkflowDatabase,
    source_path: Path,
    *,
    study_name: str,
    parent_experiment_id: str | None = None,
    parent_work_item_id: str | None = None,
    strategy: str | None = None,
    objective_name: str | None = None,
    classifications: Mapping[int, Mapping[str, Any]] | None = None,
    target_study_id: str | None = None,
) -> dict[str, Any]:
    """Import Optuna state and queue analysis.

    ``target_study_id`` attaches rows to an already scheduled/parked ATS HPO
    study. This is the safe resume path for an external optimizer: the target
    identity and source identity are checked before any durable write. Without
    a target, the historical source-hash study behavior is retained.
    """
    snapshot = read_optuna_study(source_path, study_name=study_name)
    source_path = source_path.resolve()
    stable = hashlib.sha256(
        f"{source_path}:{snapshot.source_study_id}".encode()
    ).hexdigest()[:12].upper()
    return _import_study_snapshot(
        database,
        snapshot,
        source_path=source_path,
        source_identity_path=str(source_path),
        source_study_id=snapshot.source_study_id,
        stable=stable,
        run_namespace="OPTUNA",
        provider_label="Optuna",
        study_id_prefix="OPTUNA",
        parent_experiment_id=parent_experiment_id,
        parent_work_item_id=parent_work_item_id,
        strategy=strategy,
        objective_name=objective_name,
        classifications=(
            classifications
            if classifications is not None
            else (
                EMA_V7_CLASSIFICATIONS
                if study_name.startswith("EmaConvictionTrendV7_") else {}
            )
        ),
        target_study_id=target_study_id,
    )


def import_jesse_session_export(
    database: WorkflowDatabase,
    source_path: Path,
    *,
    target_study_id: str,
    classifications: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Attach a complete Jesse session export to one existing ATS study."""
    exported = read_jesse_session_export(source_path)
    source_path = source_path.resolve()
    stable = hashlib.sha256(exported.session_id.encode()).hexdigest()[:12].upper()
    result = _import_study_snapshot(
        database,
        exported.study,
        source_path=source_path,
        source_identity_path=f"jesse-session:{exported.session_id}",
        source_study_id=0,
        stable=stable,
        run_namespace="JESSE",
        provider_label="Jesse",
        study_id_prefix="JESSE",
        classifications=classifications,
        target_study_id=target_study_id,
    )
    result["source_session_id"] = exported.session_id
    return result


def _import_study_snapshot(
    database: WorkflowDatabase,
    snapshot: OptunaStudy,
    *,
    source_path: Path,
    source_identity_path: str,
    source_study_id: int,
    stable: str,
    run_namespace: str,
    provider_label: str,
    study_id_prefix: str,
    parent_experiment_id: str | None = None,
    parent_work_item_id: str | None = None,
    strategy: str | None = None,
    objective_name: str | None = None,
    classifications: Mapping[int, Mapping[str, Any]] | None = None,
    target_study_id: str | None = None,
) -> dict[str, Any]:
    """Persist one validated optimizer snapshot and resume HPO analysis."""
    study_id = target_study_id or f"{study_id_prefix}-{stable}"
    classifications = dict(classifications or {})
    now = utc_now()
    started_at = min(
        (trial.started_at for trial in snapshot.trials if trial.started_at),
        default=None,
    )
    completed_at = max(
        (trial.completed_at for trial in snapshot.trials if trial.completed_at),
        default=None,
    )
    imported = 0
    evidence_rows = 0
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        target = connection.execute(
            "SELECT * FROM hpo_studies WHERE id=?", (study_id,),
        ).fetchone() if target_study_id else None
        if target_study_id and target is None:
            raise KeyError(f"unknown target HPO study: {target_study_id}")
        if target is not None:
            if target["lifecycle_state"] not in {
                "hpo_scheduled", "hpo_running", "hpo_analysis",
            }:
                raise ValueError(
                    "target HPO study is not awaiting optimizer import: "
                    f"{target['lifecycle_state']}"
                )
            if target["study_name"] != snapshot.name:
                raise ValueError(
                    f"{provider_label} study name does not match target HPO study: "
                    f"{snapshot.name!r} != {target['study_name']!r}"
                )
            if target["source_database_path"] is not None and (
                target["source_database_path"] != source_identity_path
                or target["source_study_id"] != source_study_id
            ):
                raise ValueError(
                    f"target HPO study already attached to another "
                    f"{provider_label} source"
                )
            existing_source = connection.execute(
                """SELECT id FROM hpo_studies
                   WHERE source_database_path=? AND source_study_id=? AND id<>?""",
                (source_identity_path, source_study_id, study_id),
            ).fetchone()
            if existing_source is not None:
                raise ValueError(
                    f"{provider_label} source is already attached to another HPO study: "
                    f"{existing_source['id']}"
                )
            parent_experiment_id = target["parent_experiment_id"]
            parent_work_item_id = target["parent_work_item_id"]
            strategy = target["strategy"] or strategy
            objective_name = target["objective_name"] or objective_name
        objective_name = objective_name or "holdout_score"
        if not parent_experiment_id or not parent_work_item_id:
            raise ValueError(
                "parent experiment/work item required when no target HPO study"
            )
        if not target_study_id and not strategy:
            raise ValueError("strategy required when no target HPO study")
        parent = connection.execute(
            """SELECT e.id,e.specification_json,w.id AS work_item_id
               FROM experiments e JOIN work_items w
                 ON w.id=? AND w.experiment_id=e.id
               WHERE e.id=?""",
            (parent_work_item_id, parent_experiment_id),
        ).fetchone()
        if parent is None:
            raise KeyError("unknown parent experiment/work item")
        parent_spec = _decode_json(parent["specification_json"])
        parent_routes = parent_spec.get("routes", [])
        parent_route = (
            dict(parent_routes[0])
            if isinstance(parent_routes, list)
            and parent_routes
            and isinstance(parent_routes[0], dict)
            else {}
        )
        if target is None:
            hpo_experiment_id = parent_experiment_id
            hpo_work_item_id = parent_work_item_id
            created_at = now
        else:
            hpo_experiment_id = target["hpo_experiment_id"]
            hpo_work_item_id = target["hpo_work_item_id"]
            created_at = target["created_at"]
        connection.execute(
            """INSERT INTO hpo_studies(
                   id,study_name,strategy,parent_experiment_id,
                   parent_work_item_id,hpo_experiment_id,hpo_work_item_id,
                   lifecycle_state,objective_name,direction,
                   source_database_path,source_study_id,trial_count,
                   completed_trial_count,started_at,completed_at,
                   created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,'hpo_analysis',?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   study_name=excluded.study_name,strategy=excluded.strategy,
                   lifecycle_state='hpo_analysis',
                   objective_name=excluded.objective_name,
                   direction=excluded.direction,
                   source_database_path=excluded.source_database_path,
                   source_study_id=excluded.source_study_id,
                   trial_count=excluded.trial_count,
                   completed_trial_count=excluded.completed_trial_count,
                   started_at=excluded.started_at,
                   completed_at=excluded.completed_at,
                   updated_at=excluded.updated_at""",
            (
                study_id, snapshot.name, strategy,
                parent_experiment_id, parent_work_item_id,
                hpo_experiment_id, hpo_work_item_id,
                objective_name, snapshot.direction,
                source_identity_path, source_study_id,
                len(snapshot.trials),
                sum(trial.state == "COMPLETE" for trial in snapshot.trials),
                started_at, completed_at, created_at, now,
            ),
        )
        connection.execute(
            "DELETE FROM hpo_selected_trials WHERE study_id=?", (study_id,),
        )
        for trial in snapshot.trials:
            run_id = f"{run_namespace}-RUN-{stable}-{trial.number}"
            session_id = f"{run_namespace.lower()}-{stable.lower()}-{trial.number}"
            route_runs = []
            for key, split in (
                ("training_metrics", "train"),
                ("testing_metrics", "holdout"),
            ):
                metrics = trial.user_attrs.get(key)
                if isinstance(metrics, dict):
                    route_runs.append({
                        "session_id": f"{session_id}-{split}",
                        "route": {
                            **parent_route,
                            "evidence_split": split,
                        },
                        "metrics": {
                            **metrics,
                            "optimizer_objective": objective_name,
                        },
                    })
            run_status = "finished" if trial.state == "COMPLETE" else "stopped"
            connection.execute(
                """INSERT INTO runs(
                       id,experiment_id,work_item_id,session_id,status,
                       metrics_json,started_at,finished_at,source_path
                   ) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       status=excluded.status,metrics_json=excluded.metrics_json,
                       started_at=excluded.started_at,
                       finished_at=excluded.finished_at,
                       source_path=excluded.source_path""",
                (
                    run_id, parent_experiment_id, parent_work_item_id,
                    session_id, run_status,
                    json.dumps({"route_runs": route_runs}, sort_keys=True),
                    trial.started_at, trial.completed_at, str(source_path),
                ),
            )
            trial_id = f"{study_id}-T{trial.number}"
            connection.execute(
                """INSERT INTO hpo_trials(
                       id,study_id,trial_number,state,objective_value,
                       started_at,completed_at,duration_ms,params_json,
                       user_attrs_json,system_attrs_json,evidence_run_id,imported_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(study_id,trial_number) DO UPDATE SET
                       state=excluded.state,
                       objective_value=excluded.objective_value,
                       started_at=excluded.started_at,
                       completed_at=excluded.completed_at,
                       duration_ms=excluded.duration_ms,
                       params_json=excluded.params_json,
                       user_attrs_json=excluded.user_attrs_json,
                       system_attrs_json=excluded.system_attrs_json,
                       evidence_run_id=excluded.evidence_run_id,
                       imported_at=excluded.imported_at""",
                (
                    trial_id, study_id, trial.number, trial.state,
                    trial.objective_value, trial.started_at, trial.completed_at,
                    _duration_ms(trial.started_at, trial.completed_at),
                    json.dumps(trial.params, sort_keys=True),
                    json.dumps(trial.user_attrs, sort_keys=True),
                    json.dumps(trial.system_attrs, sort_keys=True),
                    run_id, now,
                ),
            )
            evidence_rows += len(
                database._refresh_run_evidence(connection, run_id)
            )
            imported += 1
            classification = classifications.get(trial.number)
            if classification:
                connection.execute(
                    """INSERT INTO hpo_selected_trials(
                           study_id,trial_id,rank,classification,
                           selection_reason,selected_at
                       ) VALUES (?,?,?,?,?,?)""",
                    (
                        study_id, trial_id, classification.get("rank"),
                        classification["classification"],
                        str(classification.get("reason") or ""), now,
                    ),
                )
        _persist_candidate_defaults_and_ranges(
            connection, study_id, snapshot, classifications, now,
        )
        completed_trials = sum(
            trial.state.upper() == "COMPLETE" for trial in snapshot.trials
        )
        job = connection.execute(
            """SELECT id FROM hpo_analysis_jobs
               WHERE study_id=? AND state IN (
                 'pending','running','waiting_retry','abandoned','terminal'
               ) ORDER BY created_at DESC LIMIT 1""",
            (study_id,),
        ).fetchone()
        if job is None:
            job_id = f"HPO-ANALYSIS-{stable}"
            connection.execute(
                """INSERT INTO hpo_analysis_jobs(
                       id,study_id,state,created_at,updated_at
                   ) VALUES (?,?,'pending',?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       state='pending',updated_at=excluded.updated_at""",
                (job_id, study_id, now, now),
            )
        else:
            job_id = job["id"]
            if completed_trials:
                connection.execute(
                    """UPDATE hpo_analysis_jobs SET state='pending',
                       last_error=NULL,retry_after=NULL,claimed_by=NULL,
                       claimed_at=NULL,completed_at=NULL,updated_at=?
                       WHERE id=? AND state IN (
                         'waiting_retry','abandoned','terminal'
                       )""",
                    (now, job_id),
                )
        if completed_trials:
            # Only a durable COMPLETE trial clears the parked external
            # optimizer handoff. Keep ordinary scheduled jobs from running
            # the optimizer a second time: analysis is the next stage.
            work_item_id = hpo_work_item_id
            if work_item_id:
                work = connection.execute(
                    "SELECT specification_json,blocker_code,state FROM work_items WHERE id=?",
                    (work_item_id,),
                ).fetchone()
                parked = (
                    work is not None
                    and work["blocker_code"] == "hpo_trials_required"
                )
                imported_target = (
                    target_study_id is not None
                    and work is not None
                    and work["state"] in {
                        "scheduled", "ready", "running", "waiting_retry",
                    }
                )
                if parked or imported_target:
                    specification = _decode_json(work["specification_json"])
                    specification["readiness"] = {"status": "ready", "missing": []}
                    connection.execute(
                        """UPDATE work_items SET specification_json=?,state='finished',
                           blocker_code=CASE WHEN blocker_code='hpo_trials_required'
                               THEN NULL ELSE blocker_code END,
                           blocker_detail=CASE WHEN blocker_code='hpo_trials_required'
                               THEN NULL ELSE blocker_detail END,
                           updated_at=? WHERE id=?""",
                        (json.dumps(specification, sort_keys=True), now, work_item_id),
                    )
                    connection.execute(
                        """INSERT INTO events(
                               aggregate_type,aggregate_id,event_type,
                               payload_json,occurred_at
                           ) VALUES ('hpo_study',?,'hpo_trials_imported',?,?)""",
                        (
                            study_id,
                            json.dumps({
                                "trials_imported": imported,
                                "completed_trials": completed_trials,
                                "analysis_job_id": job_id,
                            }, sort_keys=True),
                            now,
                        ),
                    )
    return {
        "study_id": study_id,
        "source_study_id": source_study_id,
        "study_name": snapshot.name,
        "source_path": str(source_path),
        "trials_imported": imported,
        "normalized_evidence_rows": evidence_rows,
        "classifications": {
            str(number): dict(value)
            for number, value in classifications.items()
        },
        "analysis_job_id": job_id,
        "lifecycle_state": "hpo_analysis",
    }


def _persist_candidate_defaults_and_ranges(
    connection: sqlite3.Connection,
    study_id: str,
    snapshot: OptunaStudy,
    classifications: Mapping[int, Mapping[str, Any]],
    now: str,
) -> None:
    candidates = [
        trial for trial in snapshot.trials
        if classifications.get(trial.number, {}).get("classification")
        == "validation_candidate"
    ]
    candidates.sort(
        key=lambda trial: classifications[trial.number].get("rank") or 999,
    )
    connection.execute(
        "DELETE FROM hpo_proposed_defaults WHERE study_id=?", (study_id,),
    )
    connection.execute(
        "DELETE FROM hpo_narrowed_ranges WHERE study_id=?", (study_id,),
    )
    if not candidates:
        return
    source = candidates[0]
    source_id = f"{study_id}-T{source.number}"
    for name, value in source.params.items():
        connection.execute(
            """INSERT INTO hpo_proposed_defaults(
                   study_id,parameter_name,value_json,source_trial_id,
                   rationale,created_at
               ) VALUES (?,?,?,?,?,?)""",
            (
                study_id, name, json.dumps(value), source_id,
                f"Proposed only from validation candidate trial {source.number}; "
                "strategy defaults unchanged.",
                now,
            ),
        )
    names = sorted(set.intersection(*(set(trial.params) for trial in candidates)))
    for name in names:
        values = [
            float(trial.params[name]) for trial in candidates
            if isinstance(trial.params[name], (int, float))
            and not isinstance(trial.params[name], bool)
        ]
        if len(values) != len(candidates):
            continue
        distribution = source.distributions.get(name, {})
        attributes = distribution.get("attributes", {})
        connection.execute(
            """INSERT INTO hpo_narrowed_ranges(
                   study_id,parameter_name,low_value,high_value,step_value,
                   logarithmic,distribution_json,created_at
               ) VALUES (?,?,?,?,?,?,?,?)""",
            (
                study_id, name, min(values), max(values),
                attributes.get("step"),
                int(bool(attributes.get("log"))),
                json.dumps(distribution, sort_keys=True), now,
            ),
        )


def _attributes(
    connection: sqlite3.Connection,
    table: str,
    trial_id: int,
) -> dict[str, Any]:
    return {
        row["key"]: _sanitize(_decode_json(row["value_json"]))
        for row in connection.execute(
            f'SELECT "key",value_json FROM {table} WHERE trial_id=? ORDER BY "key"',
            (trial_id,),
        )
    }


def _decode_parameter(value: float, distribution: Mapping[str, Any]) -> Any:
    if distribution.get("name") == "CategoricalDistribution":
        choices = distribution.get("attributes", {}).get("choices", [])
        index = int(value)
        return choices[index] if 0 <= index < len(choices) else value
    if distribution.get("name") == "IntDistribution":
        return int(value)
    return _finite(value)


def _decode_json(value: str | None) -> Any:
    return json.loads(value) if value else {}


def _sanitize(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.replace(" ", "T") + ("Z" if "+" not in text and not text.endswith("Z") else "")


def _duration_ms(started_at: str | None, completed_at: str | None) -> int | None:
    if not started_at or not completed_at:
        return None
    from datetime import datetime
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    finish = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    return max(0, int((finish - start).total_seconds() * 1000))
