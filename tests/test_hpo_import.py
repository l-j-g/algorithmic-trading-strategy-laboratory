from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.hpo import import_optuna_study, read_optuna_study
from ats_lab.models import ExperimentSpec, ExperimentType, WorkItem, WorkState


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


if __name__ == "__main__":
    unittest.main()
