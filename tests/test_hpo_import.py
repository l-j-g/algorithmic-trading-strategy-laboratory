from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.hpo import (
    import_jesse_session_export,
    import_optuna_study,
    read_jesse_session_export,
    read_optuna_study,
)
from ats_lab.models import ExperimentSpec, ExperimentType, WorkItem, WorkState, utc_now


class OptunaImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.source = root / "optuna.sqlite3"
        source = sqlite3.connect(self.source)
        source.executescript("""
        CREATE TABLE studies(study_id INTEGER PRIMARY KEY,study_name TEXT);
        CREATE TABLE study_directions(
          study_direction_id INTEGER PRIMARY KEY,direction TEXT,
          study_id INTEGER,objective INTEGER
        );
        CREATE TABLE trials(
          trial_id INTEGER PRIMARY KEY,number INTEGER,study_id INTEGER,
          state TEXT,datetime_start TEXT,datetime_complete TEXT
        );
        CREATE TABLE trial_values(
          trial_value_id INTEGER PRIMARY KEY,trial_id INTEGER,
          objective INTEGER,value REAL,value_type TEXT
        );
        CREATE TABLE trial_params(
          param_id INTEGER PRIMARY KEY,trial_id INTEGER,param_name TEXT,
          param_value REAL,distribution_json TEXT
        );
        CREATE TABLE trial_user_attributes(
          trial_user_attribute_id INTEGER PRIMARY KEY,trial_id INTEGER,
          key TEXT,value_json TEXT
        );
        CREATE TABLE trial_system_attributes(
          trial_system_attribute_id INTEGER PRIMARY KEY,trial_id INTEGER,
          key TEXT,value_json TEXT
        );
        INSERT INTO studies VALUES (1,'Trend_optuna');
        INSERT INTO study_directions VALUES (1,'MAXIMIZE',1,0);
        INSERT INTO trials VALUES (
          1,7,1,'COMPLETE','2026-01-01 00:00:00','2026-01-01 00:00:01'
        );
        INSERT INTO trial_values VALUES (1,1,0,0.5,'FINITE');
        """)
        distribution = {
            "name": "IntDistribution",
            "attributes": {"low": 5, "high": 20, "step": 1, "log": False},
        }
        source.execute(
            "INSERT INTO trial_params VALUES (1,1,'period',12,?)",
            (json.dumps(distribution),),
        )
        source.execute(
            "INSERT INTO trial_user_attributes VALUES (1,1,'training_metrics',?)",
            (json.dumps({
                "net_profit_percentage": 12,
                "max_drawdown": -4,
                "sharpe_ratio": 1.5,
                "total_trades": 40,
            }),),
        )
        source.execute(
            "INSERT INTO trial_user_attributes VALUES (2,1,'testing_metrics',?)",
            (json.dumps({
                "net_profit_percentage": 8,
                "max_drawdown": -5,
                "sharpe_ratio": 1.1,
                "total_trades": 25,
            }),),
        )
        source.commit()
        source.close()

        self.database = WorkflowDatabase(root / "lab.sqlite3")
        self.database.initialize()
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-HPO",
            strategy_name="Trend",
            experiment_type=ExperimentType.HPO,
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-HPO",
            experiment_id="EXP-HPO",
            priority=1,
            state=WorkState.FINISHED,
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_jesse_export(self, **updates: object) -> Path:
        payload = {
            "schema_version": 1,
            "source": "jesse_optimization_session",
            "session_id": "2b1db2fb-801d-4c06-a06c-551a08f6b410",
            "study_name": "Trend_optuna",
            "direction": "maximize",
            "status": "completed",
            "trial_records_complete": True,
            "total_trials": 2,
            "completed_trials": 2,
            "trials": [{
                "number": 0,
                "state": "COMPLETE",
                "objective_value": 0.65,
                "started_at": "2026-08-01T00:00:00Z",
                "completed_at": "2026-08-01T00:00:05Z",
                "params": {"period": 12},
                "training_metrics": {
                    "net_profit_percentage": 12,
                    "sharpe_ratio": 1.5,
                    "total_trades": 40,
                },
                "testing_metrics": {
                    "net_profit_percentage": 8,
                    "sharpe_ratio": 1.1,
                    "total_trades": 25,
                },
            }, {
                "number": 1,
                "state": "COMPLETE",
                "objective_value": 0.55,
                "params": {"period": 14},
                "training_metrics": {
                    "net_profit_percentage": 10,
                    "sharpe_ratio": 1.3,
                    "total_trades": 36,
                },
                "testing_metrics": {
                    "net_profit_percentage": 7,
                    "sharpe_ratio": 1.0,
                    "total_trades": 22,
                },
            }],
            "best_candidates": [{"number": 0}],
        }
        payload.update(updates)
        path = Path(self.temp.name) / "jesse-session.json"
        path.write_text(json.dumps(payload))
        return path

    def park_target_study(self, study_id: str = "HPO-JESSE") -> str:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO hpo_studies(
                       id,study_name,strategy,parent_experiment_id,
                       parent_work_item_id,hpo_experiment_id,hpo_work_item_id,
                       lifecycle_state,objective_name,direction,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,'hpo_scheduled',?,?,?,?)""",
                (
                    study_id, "Trend_optuna", "Trend", "EXP-HPO", "JOB-HPO",
                    "EXP-HPO", "JOB-HPO", "holdout_score", "maximize", now, now,
                ),
            )
        self.database.complete_hpo_study(
            study_id, require_trial_evidence=True,
        )
        return study_id

    def test_read_and_import_without_optuna_dependency(self) -> None:
        snapshot = read_optuna_study(self.source, study_name="Trend_optuna")
        result = import_optuna_study(
            self.database,
            self.source,
            study_name="Trend_optuna",
            parent_experiment_id="EXP-HPO",
            parent_work_item_id="JOB-HPO",
            strategy="Trend",
            classifications={
                7: {
                    "classification": "validation_candidate",
                    "rank": 1,
                    "reason": "balanced",
                },
            },
        )

        self.assertEqual(snapshot.trials[0].params, {"period": 12})
        self.assertEqual(result["trials_imported"], 1)
        evidence = self.database.normalized_evidence_for_run(
            f"OPTUNA-RUN-{result['study_id'].removeprefix('OPTUNA-')}-7",
        )
        self.assertEqual(len(evidence), 2)
        self.assertEqual(
            {item.evidence_split.value for item in evidence},
            {"train", "holdout"},
        )
        self.assertEqual(
            {item.optimizer_objective for item in evidence},
            {"holdout_score"},
        )
        payload = self.database.hpo_analysis_payload(result["study_id"])
        self.assertNotIn("params", payload["trials"][0])
        self.assertEqual(
            payload["trials"][0]["evidence"][0]["schema_version"], 2,
        )
        details = self.database.diagnostic_hpo_trial_details(
            result["study_id"], 7,
        )
        self.assertEqual(details["params"], {"period": 12})

    def test_study_name_prefix_never_implies_classifications(self) -> None:
        source = sqlite3.connect(self.source)
        source.execute(
            "INSERT INTO studies VALUES (2,'EmaConvictionTrendV7_2026')",
        )
        source.execute(
            "INSERT INTO study_directions VALUES (2,'MAXIMIZE',2,0)",
        )
        source.execute(
            """INSERT INTO trials VALUES (
                   2,7,2,'COMPLETE','2026-01-01 00:00:00','2026-01-01 00:00:01'
               )""",
        )
        source.execute("INSERT INTO trial_values VALUES (2,2,0,0.4,'FINITE')")
        source.commit()
        source.close()

        result = import_optuna_study(
            self.database,
            self.source,
            study_name="EmaConvictionTrendV7_2026",
            parent_experiment_id="EXP-HPO",
            parent_work_item_id="JOB-HPO",
            strategy="Trend",
        )

        self.assertEqual(result["classifications"], {})
        self.assertEqual(self.database.rows(
            "SELECT COUNT(*) AS count FROM hpo_selected_trials WHERE study_id=?",
            (result["study_id"],),
        )[0]["count"], 0)
        self.assertEqual(self.database.rows(
            "SELECT COUNT(*) AS count FROM hpo_proposed_defaults WHERE study_id=?",
            (result["study_id"],),
        )[0]["count"], 0)

    def test_selected_trial_schedules_validation_by_reference(self) -> None:
        result = import_optuna_study(
            self.database,
            self.source,
            study_name="Trend_optuna",
            parent_experiment_id="EXP-HPO",
            parent_work_item_id="JOB-HPO",
            strategy="Trend",
            classifications={
                7: {
                    "classification": "validation_candidate",
                    "rank": 1,
                    "reason": "balanced",
                },
            },
        )

        validations = self.database.schedule_hpo_validations(
            result["study_id"], [7], evidence_splits=("oos", "rolling"),
        )

        self.assertEqual(len(validations), 2)
        self.assertEqual(
            self.database.hpo_studies(
                {"id": result["study_id"]},
            )[0]["lifecycle_state"],
            "validation",
        )
        for validation in validations:
            work = self.database.rows(
                """SELECT specification_json,blocker_code
                   FROM work_items WHERE id=?""",
                (validation["work_item_id"],),
            )[0]
            specification = json.loads(work["specification_json"])
            self.assertEqual(specification["hpo_trial_id"].endswith("-T7"), True)
            self.assertNotIn("params", specification)
            self.assertEqual(
                specification["readiness"]["status"],
                "requirements_pending",
            )
            self.assertEqual(work["blocker_code"], "requirements_pending")
            execution = self.database.execution_request(
                validation["work_item_id"],
            )
            self.assertEqual(
                execution["execution_context"]["optimizer_parameters"],
                {"period": 12},
            )
            self.assertNotIn(
                "optimizer_parameters", execution["work_item"],
            )

        configured = self.database.configure_hpo_validation_routes(
            result["study_id"],
            {
                "oos": [{
                    "exchange": "Binance Perpetual Futures",
                    "symbol": "BTC-USDT", "timeframe": "1h",
                    "start_date": "2026-01-01",
                    "finish_date": "2026-03-31",
                }],
                "rolling": [{
                    "exchange": "Binance Perpetual Futures",
                    "symbol": "ETH-USDT", "timeframe": "1h",
                    "start_date": "2025-01-01",
                    "finish_date": "2026-03-31",
                }],
            },
            updated_by="test",
        )

        self.assertEqual(len(configured["updated_work_items"]), 2)
        for validation in validations:
            work = self.database.rows(
                """SELECT blocker_code,specification_json
                   FROM work_items WHERE id=?""",
                (validation["work_item_id"],),
            )[0]
            self.assertIsNone(work["blocker_code"])
            self.assertEqual(
                json.loads(work["specification_json"])["readiness"]["status"],
                "ready",
            )
            execution = self.database.execution_request(
                validation["work_item_id"],
            )
            self.assertEqual(len(execution["experiment"]["routes"]), 1)
            self.assertEqual(
                execution["experiment"]["routes"][0]["timeframe"], "1h",
            )

        validation_id = validations[0]["work_item_id"]
        self.database.transition_work_item(
            validation_id, WorkState.FINISHED,
            allowed_from=(WorkState.SCHEDULED,),
        )
        self.assertEqual(self.database.reconcile_hpo_validation_jobs(), 1)
        projected = self.database.rows(
            "SELECT state,completed_at FROM hpo_validation_jobs "
            "WHERE work_item_id=?",
            (validation_id,),
        )[0]
        self.assertEqual(projected["state"], "finished")
        self.assertIsNotNone(projected["completed_at"])
        self.assertEqual(self.database.reconcile_hpo_validation_jobs(), 0)

    def test_validation_requires_each_requested_split_route(self) -> None:
        result = import_optuna_study(
            self.database,
            self.source,
            study_name="Trend_optuna",
            parent_experiment_id="EXP-HPO",
            parent_work_item_id="JOB-HPO",
            strategy="Trend",
            classifications={7: {"classification": "validation_candidate"}},
        )
        validations = self.database.schedule_hpo_validations(
            result["study_id"], [7], evidence_splits=("oos", "rolling"),
        )
        self.database.configure_hpo_validation_routes(
            result["study_id"],
            {"oos": [{
                "exchange": "Binance Perpetual Futures", "symbol": "BTC-USDT",
                "timeframe": "1h", "start_date": "2026-01-01",
                "finish_date": "2026-03-31",
            }]},
        )
        states = {
            row["evidence_split"]: row["readiness_status"]
            for row in self.database.hpo_study_detail(result["study_id"])["validations"]
        }
        self.assertEqual(states, {"oos": "ready", "rolling": "requirements_pending"})
        self.assertEqual(len(validations), 2)

    def test_import_resumes_existing_parked_study_without_duplicate(self) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO hpo_studies(
                       id,study_name,strategy,parent_experiment_id,
                       parent_work_item_id,hpo_experiment_id,hpo_work_item_id,
                       lifecycle_state,objective_name,direction,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,'hpo_scheduled',?,?,?,?)""",
                (
                    "HPO-PARKED", "Trend_optuna", "Trend", "EXP-HPO",
                    "JOB-HPO", "EXP-HPO", "JOB-HPO", "holdout_score",
                    "maximize", now, now,
                ),
            )
        parked = self.database.complete_hpo_study(
            "HPO-PARKED", require_trial_evidence=True,
        )
        self.assertEqual(parked["state"], "waiting_retry")
        self.assertEqual(
            self.database.rows(
                "SELECT blocker_code FROM work_items WHERE id='JOB-HPO'",
            )[0]["blocker_code"],
            "hpo_trials_required",
        )

        result = import_optuna_study(
            self.database,
            self.source,
            study_name="Trend_optuna",
            target_study_id="HPO-PARKED",
            classifications={7: {"classification": "validation_candidate"}},
        )

        self.assertEqual(result["study_id"], "HPO-PARKED")
        self.assertEqual(
            self.database.hpo_studies({"id": "HPO-PARKED"})[0]["lifecycle_state"],
            "hpo_analysis",
        )
        self.assertEqual(
            self.database.rows(
                "SELECT COUNT(*) AS count FROM hpo_studies",
            )[0]["count"],
            1,
        )
        job = self.database.rows(
            "SELECT state,last_error FROM hpo_analysis_jobs WHERE id=?",
            (parked["id"],),
        )[0]
        self.assertEqual(job["state"], "pending")
        self.assertIsNone(job["last_error"])
        work = self.database.rows(
            "SELECT state,blocker_code,specification_json FROM work_items WHERE id='JOB-HPO'",
        )[0]
        self.assertEqual(work["state"], "finished")
        self.assertIsNone(work["blocker_code"])
        self.assertEqual(json.loads(work["specification_json"])["readiness"]["status"], "ready")
        self.assertEqual(
            self.database.rows(
                """SELECT COUNT(*) AS count FROM events
                   WHERE aggregate_id='HPO-PARKED' AND event_type='hpo_trials_imported'""",
            )[0]["count"],
            1,
        )

    def test_import_accepts_jesse_optuna_alias_for_parked_study(self) -> None:
        target = self.park_target_study("HPO-ALIAS")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE hpo_studies SET study_name='Trend-HPO-ALIAS' WHERE id=?",
                (target,),
            )
        source = sqlite3.connect(self.source)
        source.execute(
            "UPDATE studies SET study_name=? WHERE study_id=1",
            ("Trend_optuna_ray_9bff6b0c-e699-453c-bd8d-c32c6ded2c66",),
        )
        source.commit()
        source.close()

        result = import_optuna_study(
            self.database,
            self.source,
            study_name="Trend_optuna_ray_9bff6b0c-e699-453c-bd8d-c32c6ded2c66",
            target_study_id=target,
        )

        self.assertEqual(result["study_name"], "Trend-HPO-ALIAS")
        self.assertEqual(
            result["source_study_name"],
            "Trend_optuna_ray_9bff6b0c-e699-453c-bd8d-c32c6ded2c66",
        )
        study = self.database.rows(
            "SELECT study_name,lifecycle_state FROM hpo_studies WHERE id=?",
            (target,),
        )[0]
        self.assertEqual(study["study_name"], "Trend-HPO-ALIAS")
        self.assertEqual(study["lifecycle_state"], "hpo_analysis")

    def test_complete_jesse_session_export_resumes_parked_study(self) -> None:
        source = self.write_jesse_export()
        target = self.park_target_study()

        snapshot = read_jesse_session_export(source)
        result = import_jesse_session_export(
            self.database, source, target_study_id=target,
        )
        repeated = import_jesse_session_export(
            self.database, source, target_study_id=target,
        )

        self.assertEqual(snapshot.session_id, result["source_session_id"])
        self.assertEqual(result["trials_imported"], 2)
        self.assertEqual(result["normalized_evidence_rows"], 4)
        self.assertEqual(repeated["trials_imported"], 2)
        self.assertEqual(
            self.database.rows(
                "SELECT COUNT(*) AS count FROM hpo_trials WHERE study_id=?",
                (target,),
            )[0]["count"],
            2,
        )
        study = self.database.rows(
            "SELECT * FROM hpo_studies WHERE id=?", (target,),
        )[0]
        self.assertEqual(study["completed_trial_count"], 2)
        self.assertEqual(
            study["source_database_path"],
            "jesse-session:2b1db2fb-801d-4c06-a06c-551a08f6b410",
        )
        self.assertEqual(study["lifecycle_state"], "hpo_analysis")
        self.assertIsNone(self.database.rows(
            "SELECT blocker_code FROM work_items WHERE id='JOB-HPO'",
        )[0]["blocker_code"])

    def test_jesse_best_candidates_only_export_is_rejected(self) -> None:
        source = self.write_jesse_export(
            trial_records_complete=False,
            trials=None,
            best_candidates=[{"number": 0, "fitness": 0.65}],
        )
        target = self.park_target_study()

        with self.assertRaisesRegex(
            ValueError, "best_candidates is partial; full trials export required",
        ):
            import_jesse_session_export(
                self.database, source, target_study_id=target,
            )

        self.assertEqual(self.database.rows(
            "SELECT COUNT(*) AS count FROM hpo_trials WHERE study_id=?",
            (target,),
        )[0]["count"], 0)

    def test_jesse_incomplete_trial_array_is_rejected_without_writes(self) -> None:
        full = json.loads(self.write_jesse_export().read_text())
        source = self.write_jesse_export(trials=full["trials"][:1])
        target = self.park_target_study()

        with self.assertRaisesRegex(ValueError, "trial counts must match"):
            import_jesse_session_export(
                self.database, source, target_study_id=target,
            )

        study = self.database.rows(
            "SELECT * FROM hpo_studies WHERE id=?", (target,),
        )[0]
        self.assertEqual(study["trial_count"], 0)
        self.assertIsNone(study["source_database_path"])

    def test_import_identity_keys_on_content_not_path(self) -> None:
        first = import_optuna_study(
            self.database,
            self.source,
            study_name="Trend_optuna",
            parent_experiment_id="EXP-HPO",
            parent_work_item_id="JOB-HPO", strategy="Trend",
        )
        copy_path = Path(self.temp.name) / "copy.sqlite3"
        copy_path.write_bytes(self.source.read_bytes())
        other = WorkflowDatabase(Path(self.temp.name) / "other.sqlite3")
        other.initialize()
        other.upsert_experiment(ExperimentSpec(
            id="EXP-OTHER", strategy_name="Trend",
            experiment_type=ExperimentType.HPO,
        ))
        other.upsert_work_item(WorkItem(
            id="JOB-OTHER", experiment_id="EXP-OTHER", priority=1,
            state=WorkState.FINISHED,
        ))
        second = import_optuna_study(
            other,
            copy_path,
            study_name="Trend_optuna",
            parent_experiment_id="EXP-OTHER",
            parent_work_item_id="JOB-OTHER", strategy="Trend",
        )
        self.assertEqual(first["study_id"], second["study_id"])

        source = sqlite3.connect(self.source)
        source.execute(
            """INSERT INTO trials VALUES (
                   3,8,1,'COMPLETE','2026-01-01 00:00:00','2026-01-01 00:00:02'
               )""",
        )
        source.execute("INSERT INTO trial_values VALUES (3,3,0,0.6,'FINITE')")
        source.commit()
        source.close()
        third = import_optuna_study(
            other,
            self.source,
            study_name="Trend_optuna",
            parent_experiment_id="EXP-OTHER",
            parent_work_item_id="JOB-OTHER", strategy="Trend",
        )
        self.assertNotEqual(third["study_id"], first["study_id"])

    def test_read_rejects_partial_optuna_schema(self) -> None:
        partial = Path(self.temp.name) / "partial.sqlite3"
        sqlite3.connect(partial).close()
        with self.assertRaisesRegex(ValueError, "missing table studies"):
            read_optuna_study(partial, study_name="Trend_optuna")


if __name__ == "__main__":
    unittest.main()
