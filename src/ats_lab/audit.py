"""Migration completeness and ambiguity reporting."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .database import WorkflowDatabase


def build_audit(database: WorkflowDatabase) -> dict:
    experiments = database.rows("SELECT COUNT(*) AS count FROM experiments")[0]["count"]
    work_items = database.rows("SELECT state, COUNT(*) AS count FROM work_items GROUP BY state")
    evaluations = database.rows("SELECT verdict, COUNT(*) AS count FROM evaluations GROUP BY verdict")
    runs = database.rows("SELECT status, COUNT(*) AS count FROM runs GROUP BY status")
    missing_strategy = database.rows("SELECT COUNT(*) AS count FROM experiments e JOIN strategies s ON s.id=e.strategy_id WHERE s.name='unknown'")[0]["count"]
    finished_missing_evaluation = database.rows(
        """SELECT COUNT(*) AS count FROM experiments e
           JOIN work_items w ON w.experiment_id=e.id
           LEFT JOIN evaluations v ON v.experiment_id=e.id
           WHERE w.state = 'finished' AND v.id IS NULL"""
    )[0]["count"]
    active_unevaluated = database.rows(
        """SELECT COUNT(*) AS count FROM experiments e
           JOIN work_items w ON w.experiment_id=e.id
           LEFT JOIN evaluations v ON v.experiment_id=e.id
           WHERE w.state IN ('scheduled','ready','running','waiting_retry','blocked') AND v.id IS NULL"""
    )[0]["count"]
    archived_unevaluated = database.rows(
        """SELECT COUNT(*) AS count FROM experiments e
           JOIN work_items w ON w.experiment_id=e.id
           LEFT JOIN evaluations v ON v.experiment_id=e.id
           WHERE w.state = 'archived' AND v.id IS NULL"""
    )[0]["count"]
    active = database.rows("SELECT COUNT(*) AS count FROM active_queue")[0]["count"]
    return {
        "schema_version": database.rows("SELECT MAX(version) AS version FROM schema_migrations")[0]["version"],
        "experiments": experiments,
        "active_queue": active,
        "work_states": {row["state"]: row["count"] for row in work_items},
        "verdicts": {row["verdict"]: row["count"] for row in evaluations},
        "run_statuses": {row["status"]: row["count"] for row in runs},
        "ambiguities": {
            "unknown_strategy": missing_strategy,
            "finished_missing_evaluation": finished_missing_evaluation,
        },
        "expected_incomplete": {
            "active_without_evaluation": active_unevaluated,
            "archived_without_evaluation": archived_unevaluated,
        },
    }


def render_markdown(audit: dict) -> str:
    lines = [
        "# Workflow V2 Migration Audit", "",
        f"Schema version: `{audit['schema_version']}`", "",
        f"Experiments imported: **{audit['experiments']}**", "",
        f"Active queue items: **{audit['active_queue']}**", "",
        "## Work states", "",
    ]
    lines.extend(f"- `{key}`: {value}" for key, value in sorted(audit["work_states"].items()))
    lines.extend(["", "## Verdicts", ""])
    lines.extend(f"- `{key}`: {value}" for key, value in sorted(audit["verdicts"].items()))
    lines.extend(["", "## Run statuses", ""])
    lines.extend(f"- `{key}`: {value}" for key, value in sorted(audit["run_statuses"].items()))
    lines.extend(["", "## Ambiguities requiring review", ""])
    lines.extend(f"- `{key}`: {value}" for key, value in sorted(audit["ambiguities"].items()))
    lines.extend(["", "## Expected incomplete work", ""])
    lines.extend(f"- `{key}`: {value}" for key, value in sorted(audit["expected_incomplete"].items()))
    lines.extend(["", "Legacy migration complete; Markdown sidecars retired.", ""])
    return "\n".join(lines)
