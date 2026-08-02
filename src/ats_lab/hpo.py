"""Read-only Optuna SQLite import into durable ATS Lab HPO state."""
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
            trials.append(OptunaTrial(
                number=int(row["number"]),
                state=str(row["state"]),
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


def import_optuna_study(
    database: WorkflowDatabase,
    source_path: Path,
    *,
    study_name: str,
    parent_experiment_id: str,
    parent_work_item_id: str,
    strategy: str,
    objective_name: str = "holdout_score",
    classifications: Mapping[int, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Import Optuna state, normalized trial evidence, and analyzer handoff."""
    snapshot = read_optuna_study(source_path, study_name=study_name)
    source_path = source_path.resolve()
    stable = hashlib.sha256(
        f"{source_path}:{snapshot.source_study_id}".encode()
    ).hexdigest()[:12].upper()
    study_id = f"OPTUNA-{stable}"
    classifications = dict(
        classifications
        if classifications is not None
        else (
            EMA_V7_CLASSIFICATIONS
            if study_name.startswith("EmaConvictionTrendV7_") else {}
        )
    )
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
                parent_experiment_id, parent_work_item_id,
                objective_name, snapshot.direction,
                str(source_path), snapshot.source_study_id,
                len(snapshot.trials),
                sum(trial.state == "COMPLETE" for trial in snapshot.trials),
                started_at, completed_at, now, now,
            ),
        )
        connection.execute(
            "DELETE FROM hpo_selected_trials WHERE study_id=?", (study_id,),
        )
        for trial in snapshot.trials:
            run_id = f"OPTUNA-RUN-{stable}-{trial.number}"
            session_id = f"optuna-{stable.lower()}-{trial.number}"
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
        job = connection.execute(
            """SELECT id FROM hpo_analysis_jobs
               WHERE study_id=? AND state IN (
                 'pending','running','waiting_retry','abandoned'
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
    return {
        "study_id": study_id,
        "source_study_id": snapshot.source_study_id,
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
