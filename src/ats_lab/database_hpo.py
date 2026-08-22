"""HPO study lifecycle: studies, trials, validation routing, analysis jobs."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Mapping

from .database_support import _json_object
from .models import RouteSpec, utc_now


def _validate_hpo_route_partitions(routes_by_split: Mapping[str, list[dict[str, str]]]) -> None:
    """Reject malformed or train/validation-overlapping HPO route files."""
    parsed: dict[str, list[tuple[dict[str, str], date, date]]] = {}
    for split, routes in routes_by_split.items():
        parsed[split] = []
        for route in routes:
            try:
                start = date.fromisoformat(route["start_date"])
                finish = date.fromisoformat(route["finish_date"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(
                    f"route dates must use YYYY-MM-DD: {split}"
                ) from error
            if start >= finish:
                raise ValueError(
                    f"route start_date must precede finish_date: {split}"
                )
            parsed[split].append((route, start, finish))
    training = parsed.get("hpo", [])
    for train, train_start, train_finish in training:
        for split in ("oos", "rolling"):
            for validation, validation_start, validation_finish in parsed.get(split, []):
                same_market = all(
                    train.get(field) == validation.get(field)
                    for field in ("exchange", "symbol", "timeframe")
                )
                overlaps = (
                    train_start < validation_finish
                    and validation_start < train_finish
                )
                if same_market and overlaps:
                    raise ValueError(
                        f"{split} route overlaps hpo training for "
                        f"{validation.get('symbol')} {validation.get('timeframe')}"
                    )


_HPO_STUDY_FILTER_ALIASES = {
    "id": "study_id",
    "study_name": "name",
}


def _matches_hpo_study_filters(
    row: Mapping[str, object],
    filters: Mapping[str, object],
) -> bool:
    """Python-side twin of the SQL WHERE clause built for hpo_studies.

    A None filter means IS NULL (the row key must be present and None);
    any other filter is plain equality.
    """
    for field, value in filters.items():
        actual = row.get(_HPO_STUDY_FILTER_ALIASES.get(field, field))
        if value is None:
            if actual is not None:
                return False
        elif actual != value:
            return False
    return True


class HpoMixin:
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
                if _matches_hpo_study_filters(candidate, filters):
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

    def hpo_studies_needing_default_routes(self) -> list[dict]:
        """Return scheduled HPO studies with no operator route choices.

        Partial route files are intentionally excluded. They represent an
        operator decision and must not be silently overwritten by bootstrap
        policy.
        """
        rows = self.rows(
            """SELECT s.id AS study_id,s.strategy,s.lifecycle_state,
                      e.specification_json AS experiment_json,
                      w.state AS work_state
                 FROM hpo_studies s
                 JOIN experiments e ON e.id=s.hpo_experiment_id
                 JOIN work_items w ON w.id=s.hpo_work_item_id
                WHERE s.lifecycle_state='hpo_scheduled'
                  AND w.state IN ('scheduled','ready','running')
                ORDER BY s.updated_at,s.id"""
        )
        eligible = []
        for row in rows:
            specification = _json_object(row.get("experiment_json"))
            routes = specification.get("routes")
            validation = specification.get("validation_routes")
            if isinstance(routes, list) and routes:
                continue
            if isinstance(validation, dict) and any(
                isinstance(value, list) and value
                for value in validation.values()
            ):
                continue
            eligible.append({
                key: row.get(key)
                for key in ("study_id", "strategy", "lifecycle_state", "work_state")
            })
        return eligible

    def configure_default_hpo_routes(
        self,
        study_id: str,
        routes_by_split: Mapping[str, object],
        *,
        updated_by: str = "ats-lab-defaults",
    ) -> dict:
        """Apply bootstrap routes only to an untouched scheduled study."""
        if not any(item.get("study_id") == study_id
                   for item in self.hpo_studies_needing_default_routes()):
            raise ValueError(
                "default routes only apply to a scheduled HPO study with no routes"
            )
        return self.configure_hpo_validation_routes(
            study_id, routes_by_split, updated_by=updated_by,
        )

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
                    validation_routes = specification.get("validation_routes")
                    routes = (
                        validation_routes.get(split)
                        if isinstance(validation_routes, dict)
                        else None
                    )
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
        """Attach split-specific routes and release pending HPO work.

        Validation jobs are normally created before routes are known.  A
        scheduled HPO study can also reach this command before validation
        jobs exist.  An explicit ``hpo`` route entry then attaches only that
        route to the optimizer; OOS/rolling entries never leak into HPO
        execution.  This keeps route readiness explicit while avoiding a
        second operator-only SQLite edit path.
        """
        normalized: dict[str, list[dict[str, str]]] = {}
        for split, raw_routes in routes_by_split.items():
            if split not in {"oos", "rolling", "hpo"}:
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
        _validate_hpo_route_partitions(normalized)
        now = utc_now()
        updated = []
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            study = connection.execute(
                """SELECT id,hpo_experiment_id,hpo_work_item_id
                   FROM hpo_studies WHERE id=?""",
                (study_id,),
            ).fetchone()
            if study is None:
                raise KeyError(f"unknown HPO study: {study_id}")
            existing_experiment = connection.execute(
                "SELECT specification_json FROM experiments WHERE id=?",
                (study["hpo_experiment_id"],),
            ).fetchone()
            existing_specification = _json_object(
                existing_experiment["specification_json"]
                if existing_experiment is not None else None,
            )
            existing_validation = existing_specification.get("validation_routes")
            combined_routes = {
                split: list(value)
                for split, value in existing_validation.items()
            } if isinstance(existing_validation, dict) else {}
            combined_routes.update({
                split: routes for split, routes in normalized.items()
                if split != "hpo"
            })
            if "hpo" in normalized:
                combined_routes["hpo"] = normalized["hpo"]
            else:
                existing_routes = existing_specification.get("routes")
                combined_routes["hpo"] = (
                    [dict(route) for route in existing_routes
                     if isinstance(route, dict)]
                    if isinstance(existing_routes, list) else []
                )
            _validate_hpo_route_partitions(combined_routes)
            # Keep the study's route projection complete. Validation jobs may
            # not exist yet, so storing only their child experiment metadata
            # would make `hpo-route-plan` report false missing splits.
            existing_specification["validation_routes"] = {
                split: routes
                for split, routes in combined_routes.items()
                if split in {"oos", "rolling"} and routes
            }
            if combined_routes.get("hpo"):
                existing_specification["routes"] = combined_routes["hpo"]
            connection.execute(
                """UPDATE experiments SET specification_json=?,updated_at=?
                   WHERE id=?""",
                (
                    json.dumps(existing_specification, sort_keys=True),
                    now, study["hpo_experiment_id"],
                ),
            )
            hpo_routes = [
                route
                for split in ("hpo",)
                for route in normalized.get(split, [])
            ]
            validation_jobs_found = False
            for split, routes in normalized.items():
                if split == "hpo":
                    continue
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
                validation_jobs_found = validation_jobs_found or bool(jobs)
                for job in jobs:
                    experiment = _json_object(job["experiment_json"])
                    experiment["routes"] = routes
                    configured = experiment.get("validation_routes")
                    validation_routes = (
                        dict(configured) if isinstance(configured, dict) else {}
                    )
                    validation_routes[split] = routes
                    experiment["validation_routes"] = validation_routes
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
            # A study may still be hpo_scheduled with no selected trials and
            # therefore no validation jobs.  Release its optimizer work item
            # once canonical routes are supplied instead of leaving it in a
            # permanent requirements_pending state.
            hpo_work = connection.execute(
                """SELECT specification_json,state FROM work_items WHERE id=?""",
                (study["hpo_work_item_id"],),
            ).fetchone()
            hpo_operation = (
                _json_object(hpo_work["specification_json"]).get("operation")
                if hpo_work is not None else None
            )
            hpo_experiment = connection.execute(
                """SELECT specification_json FROM experiments WHERE id=?""",
                (study["hpo_experiment_id"],),
            ).fetchone()
            if hpo_experiment is not None and hpo_routes and hpo_operation == "hpo":
                experiment = _json_object(hpo_experiment["specification_json"])
                existing_routes = experiment.get("routes")
                if not isinstance(existing_routes, list) or not existing_routes:
                    experiment["routes"] = hpo_routes
                    connection.execute(
                        """UPDATE experiments SET specification_json=?,updated_at=?
                           WHERE id=?""",
                        (
                            json.dumps(experiment, sort_keys=True),
                            now, study["hpo_experiment_id"],
                        ),
                    )
                if hpo_work is not None:
                    work = _json_object(hpo_work["specification_json"])
                    work["readiness"] = {"status": "ready", "missing": []}
                    connection.execute(
                        """UPDATE work_items SET specification_json=?,
                           state=CASE WHEN state='blocked'
                                AND blocker_code='requirements_pending'
                              THEN 'scheduled' ELSE state END,
                           blocker_code=NULL,blocker_detail=NULL,updated_at=?
                           WHERE id=?""",
                        (
                            json.dumps(work, sort_keys=True),
                            now, study["hpo_work_item_id"],
                        ),
                    )
                    updated.append(study["hpo_work_item_id"])
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
                        "validation_jobs_found": validation_jobs_found,
                        "hpo_routes": len(hpo_routes),
                    }, sort_keys=True),
                    now,
                ),
            )
        return {
            "study_id": study_id,
            "updated_work_items": sorted(updated),
            "hpo_routes": len(hpo_routes),
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
        require_trial_evidence: bool = False,
    ) -> dict:
        """Move a finished study into durable analyzer queue atomically.

        Scheduled optimizer runs are expected to persist/import trial rows
        before analysis.  When ``require_trial_evidence`` is enabled and the
        run produced no completed trials, park the analyzer handoff instead
        of repeatedly claiming an empty payload.  An external optimizer
        import can later reopen the parked job after durable trials exist.
        """
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
            missing_trials = require_trial_evidence and not int(
                counts["complete"] or 0
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
                           id,study_id,state,last_error,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?)""",
                    (
                        job_id, study_id,
                        "waiting_retry" if missing_trials else "pending",
                        (
                            "hpo_trials_required: import completed optimizer "
                            "trials before HPO analysis"
                            if missing_trials else None
                        ),
                        now, now,
                    ),
                )
                job = connection.execute(
                    "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job_id,),
                ).fetchone()
            elif missing_trials:
                connection.execute(
                    """UPDATE hpo_analysis_jobs SET state='waiting_retry',
                       last_error=?,retry_after=NULL,claimed_by=NULL,
                       claimed_at=NULL,updated_at=? WHERE id=?""",
                    (
                        "hpo_trials_required: import completed optimizer "
                        "trials before HPO analysis",
                        now, job["id"],
                    ),
                )
                job = connection.execute(
                    "SELECT * FROM hpo_analysis_jobs WHERE id=?", (job["id"],),
                ).fetchone()
            if missing_trials:
                work_item_id = study["hpo_work_item_id"]
                if work_item_id:
                    work = connection.execute(
                        "SELECT specification_json FROM work_items WHERE id=?",
                        (work_item_id,),
                    ).fetchone()
                    specification = _json_object(
                        work["specification_json"] if work else "{}"
                    )
                    specification["readiness"] = {
                        "status": "requirements_pending",
                        "missing": ["hpo_trials"],
                    }
                    connection.execute(
                        """UPDATE work_items SET specification_json=?,
                           blocker_code='hpo_trials_required',
                           blocker_detail='Import completed optimizer trials before HPO analysis',
                           updated_at=? WHERE id=?""",
                        (json.dumps(specification, sort_keys=True), now, work_item_id),
                    )
                    connection.execute(
                        """INSERT INTO events(
                               aggregate_type,aggregate_id,event_type,
                               payload_json,occurred_at
                           ) VALUES ('hpo_study',?,'hpo_trials_required',?,?)""",
                        (
                            study_id,
                            json.dumps({
                                "next_action": (
                                    "Import completed optimizer trials, then "
                                    "resume HPO analysis."
                                ),
                            }, sort_keys=True),
                            now,
                        ),
                    )
            return dict(job)

    def reconcile_finished_hpo_work(self, *, limit: int = 100) -> list[str]:
        """Repair HPO studies left running after terminal execution handling.

        Older supervisor paths could finish an HPO work item through failure
        analysis without creating the analyzer handoff. Only finished HPO work
        is eligible; active optimizer claims remain untouched.
        """
        rows = self.rows(
            """SELECT s.id AS study_id
                 FROM hpo_studies s
                 JOIN work_items w ON w.id=s.hpo_work_item_id
                WHERE s.lifecycle_state='hpo_running'
                  AND w.state='finished'
                ORDER BY s.updated_at,s.id LIMIT ?""",
            (max(1, min(int(limit), 1000)),),
        )
        repaired: list[str] = []
        for row in rows:
            self.complete_hpo_study(
                str(row["study_id"]), require_trial_evidence=True,
            )
            repaired.append(str(row["study_id"]))
        return repaired

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

    def requeue_hpo_execution(
        self,
        study_id: str,
        *,
        reason: str,
        updated_by: str = "operator",
    ) -> dict:
        """Reopen a trial-less optimizer after its provider is repaired."""
        reason = reason.strip()
        if not reason:
            raise ValueError("requeue reason is required")
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            study = connection.execute(
                """SELECT * FROM hpo_studies WHERE id=?""", (study_id,),
            ).fetchone()
            if study is None:
                raise KeyError(f"unknown HPO study: {study_id}")
            if study["lifecycle_state"] != "hpo_analysis":
                raise ValueError(
                    f"HPO study is not parked for execution: {study_id}"
                )
            complete_trials = connection.execute(
                """SELECT COUNT(*) AS count FROM hpo_trials
                   WHERE study_id=? AND state='COMPLETE'""", (study_id,),
            ).fetchone()["count"]
            if int(complete_trials or 0):
                raise ValueError(
                    f"HPO study already has completed trials: {study_id}"
                )
            work = connection.execute(
                """SELECT * FROM work_items WHERE id=?""",
                (study["hpo_work_item_id"],),
            ).fetchone()
            if work is None:
                raise KeyError(f"missing HPO work item: {study_id}")
            if work["blocker_code"] != "hpo_trials_required":
                raise ValueError(
                    f"HPO study is not waiting for trial evidence: {study_id}"
                )
            specification = _json_object(work["specification_json"])
            specification["readiness"] = {"status": "ready", "missing": []}
            connection.execute(
                """UPDATE work_items SET state='ready',attempts=0,
                   retry_after=NULL,blocker_code=NULL,blocker_detail=NULL,
                   claimed_by=NULL,claimed_at=NULL,specification_json=?,updated_at=?
                   WHERE id=?""",
                (json.dumps(specification, sort_keys=True), now, work["id"]),
            )
            connection.execute(
                """UPDATE hpo_studies SET lifecycle_state='hpo_scheduled',
                   started_at=NULL,completed_at=NULL,updated_at=? WHERE id=?""",
                (now, study_id),
            )
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES ('hpo_study',?,'hpo_execution_requeued',?,?)""",
                (study_id, json.dumps({
                    "reason": reason,
                    "updated_by": updated_by,
                    "work_item_id": work["id"],
                }, sort_keys=True), now),
            )
            return {
                "study_id": study_id,
                "lifecycle_state": "hpo_scheduled",
                "work_item_id": work["id"],
                "work_state": "ready",
            }

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
