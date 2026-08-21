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


if __name__ == "__main__":
    unittest.main()
