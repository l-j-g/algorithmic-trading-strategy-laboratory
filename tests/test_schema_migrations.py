from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ats_lab import SCHEMA_VERSION
from ats_lab.database import WorkflowDatabase


def _schema_snapshot(database: WorkflowDatabase) -> dict:
    tables = {}
    views = {}
    indexes = {}
    for row in database.rows(
        """SELECT type,name,tbl_name,sql FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%'"""
    ):
        if row["type"] == "table":
            tables[row["name"]] = sorted(
                (column["name"], column["type"], column["notnull"],
                 column["dflt_value"], column["pk"])
                for column in database.rows(f"PRAGMA table_info({row['name']})")
            )
        elif row["type"] == "view":
            views[row["name"]] = row["sql"]
        elif row["type"] == "index":
            indexes[row["name"]] = (row["tbl_name"], row["sql"])
    return {"tables": tables, "views": views, "indexes": indexes}


def _degrade_to_pre_migration_layout(path: Path) -> None:
    """Rewind a fresh database to the pre-v2 column layout."""
    connection = sqlite3.connect(path)
    try:
        connection.executescript("""
            ALTER TABLE runs DROP COLUMN raw_result_json;
            ALTER TABLE direct_execution_sessions DROP COLUMN first_observed_at;
            ALTER TABLE direct_execution_sessions DROP COLUMN last_observed_at;
            ALTER TABLE direct_execution_sessions DROP COLUMN last_jesse_updated_at;
            ALTER TABLE direct_execution_sessions DROP COLUMN last_progress;
            ALTER TABLE direct_execution_sessions DROP COLUMN unchanged_observations;
            ALTER TABLE direct_execution_sessions DROP COLUMN recovery_attempted;
            ALTER TABLE direct_execution_sessions DROP COLUMN replacement_created;
            ALTER TABLE normalized_evidence DROP COLUMN monte_carlo_scenarios;
            ALTER TABLE normalized_evidence DROP COLUMN monte_carlo_method;
            ALTER TABLE normalized_evidence DROP COLUMN walk_forward_windows;
            ALTER TABLE normalized_evidence DROP COLUMN walk_forward_method;
            ALTER TABLE normalized_evidence DROP COLUMN leverage_mode;
            ALTER TABLE normalized_evidence DROP COLUMN configured_futures_leverage;
            ALTER TABLE normalized_evidence DROP COLUMN effective_leverage_mean;
            ALTER TABLE normalized_evidence DROP COLUMN effective_leverage_p95;
            ALTER TABLE normalized_evidence DROP COLUMN effective_leverage_max;
            ALTER TABLE normalized_evidence DROP COLUMN liquidation_count;
            DROP VIEW candidate_summary;
            DROP VIEW evaluations;
            ALTER TABLE evaluation_history RENAME TO legacy_history;
            CREATE TABLE evaluations (
                id INTEGER PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                verdict TEXT NOT NULL CHECK (verdict IN ('reject','revise','hpo_candidate','paper_trade_candidate','inconclusive','infrastructure_failure','pass')),
                summary TEXT NOT NULL DEFAULT '',
                metrics_summary TEXT NOT NULL DEFAULT '',
                next_step TEXT NOT NULL DEFAULT '',
                gate_results_json TEXT NOT NULL DEFAULT '[]',
                evaluator TEXT NOT NULL,
                evaluated_at TEXT NOT NULL,
                UNIQUE(experiment_id, evaluator, evaluated_at)
            );
            INSERT INTO evaluations(id,experiment_id,verdict,summary,metrics_summary,
               next_step,gate_results_json,evaluator,evaluated_at)
               SELECT id,experiment_id,verdict,summary,metrics_summary,
                      next_step,gate_results_json,evaluator,evaluated_at
               FROM legacy_history;
            DROP TABLE legacy_history;
            DELETE FROM schema_migrations;
        """)
        connection.commit()
    finally:
        connection.close()


class SchemaMigrationTests(unittest.TestCase):
    def test_indexes_from_minor_register_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()

            indexes = {
                row["name"]
                for row in database.rows(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertIn("idx_runs_work_item", indexes)
            self.assertIn("idx_events_aggregate", indexes)

    def test_fresh_and_stepwise_migrated_databases_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = WorkflowDatabase(Path(tmp) / "fresh.sqlite3")
            fresh.initialize()
            expected = _schema_snapshot(fresh)

            legacy_path = Path(tmp) / "legacy.sqlite3"
            seed = WorkflowDatabase(legacy_path)
            seed.initialize()
            _degrade_to_pre_migration_layout(legacy_path)

            migrated = WorkflowDatabase(legacy_path)
            migrated.initialize()

            self.assertEqual(_schema_snapshot(migrated), expected)

    def test_initialize_records_every_migration_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            first = database.rows(
                "SELECT version,applied_at FROM schema_migrations ORDER BY version"
            )
            database.initialize()
            second = database.rows(
                "SELECT version,applied_at FROM schema_migrations ORDER BY version"
            )

            self.assertEqual(
                [row["version"] for row in first],
                list(range(2, SCHEMA_VERSION + 1)),
            )
            self.assertEqual(first, second)

    def test_append_only_migration_preserves_verdict_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.executescript("""
                    CREATE TABLE experiments (
                        id TEXT PRIMARY KEY,
                        experiment_type TEXT NOT NULL,
                        specification_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE schema_migrations (
                        version INTEGER PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    );
                    CREATE TABLE evaluations (
                        id INTEGER PRIMARY KEY,
                        experiment_id TEXT NOT NULL REFERENCES experiments(id),
                        verdict TEXT NOT NULL CHECK (verdict IN ('reject','revise','hpo_candidate','paper_trade_candidate','inconclusive','infrastructure_failure','pass')),
                        summary TEXT NOT NULL DEFAULT '',
                        metrics_summary TEXT NOT NULL DEFAULT '',
                        next_step TEXT NOT NULL DEFAULT '',
                        gate_results_json TEXT NOT NULL DEFAULT '[]',
                        evaluator TEXT NOT NULL,
                        evaluated_at TEXT NOT NULL,
                        UNIQUE(experiment_id, evaluator, evaluated_at)
                    );
                    INSERT INTO experiments(id,experiment_type,specification_json,created_at,updated_at)
                        VALUES ('EXP-1','baseline','{}','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z');
                    INSERT INTO schema_migrations(version,applied_at) VALUES (6,'2026-01-01T00:00:00Z');
                    INSERT INTO evaluations(experiment_id,verdict,summary,evaluator,evaluated_at) VALUES
                        ('EXP-1','reject','first revision','analyzer','2026-01-02T00:00:00Z'),
                        ('EXP-1','pass','second revision','analyzer','2026-01-03T00:00:00Z'),
                        ('EXP-1','inconclusive','other evaluator','operator','2026-01-04T00:00:00Z');
                """)
                connection.commit()
            finally:
                connection.close()

            database = WorkflowDatabase(path)
            database.initialize()

            history = database.rows(
                """SELECT evaluator,verdict,sequence,superseded_at
                   FROM evaluation_history ORDER BY id"""
            )
            self.assertEqual(
                [(row["evaluator"], row["verdict"], row["sequence"]) for row in history],
                [
                    ("analyzer", "reject", 0),
                    ("analyzer", "pass", 1),
                    ("operator", "inconclusive", 0),
                ],
            )
            self.assertIsNotNone(history[0]["superseded_at"])
            self.assertIsNone(history[1]["superseded_at"])
            self.assertIsNone(history[2]["superseded_at"])
            visible = database.rows(
                "SELECT verdict FROM evaluations ORDER BY evaluator"
            )
            self.assertEqual(
                [row["verdict"] for row in visible], ["pass", "inconclusive"],
            )


if __name__ == "__main__":
    unittest.main()
