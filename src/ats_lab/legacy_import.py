"""Idempotent import adapter for workflow-v1 Markdown and JSON evidence."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .database import WorkflowDatabase
from . import legacy_adapter
from .models import (
    Evaluation,
    ExperimentSpec,
    ExperimentType,
    RouteSpec,
    RunResult,
    RunStatus,
    Verdict,
    WorkItem,
    WorkState,
    utc_now,
)


def _priority(value: str) -> int:
    return {"P0": 0, "P1": 10, "P2": 20, "P3": 30}.get(value, 90)


def _experiment_type(job_id: str, text: str) -> ExperimentType:
    value = f"{job_id} {text}".lower()
    checks = (
        (("monte carlo", "-mc-"), ExperimentType.MONTE_CARLO),
        (("significance", "-sig-", "rst"), ExperimentType.SIGNIFICANCE),
        (("hpo",), ExperimentType.HPO),
        (("cost", "fee"), ExperimentType.COST_SENSITIVITY),
        (("out of sample", "oos"), ExperimentType.OUT_OF_SAMPLE),
        (("multi-window", "multi window", "-mw-"), ExperimentType.MULTI_WINDOW),
        (("fix", "harness", "preflight"), ExperimentType.HARNESS_CHECK),
        (("baseline", "-bl-"), ExperimentType.BASELINE),
    )
    for tokens, kind in checks:
        if any(token in value for token in tokens):
            return kind
    return ExperimentType.UNKNOWN


def _work_state(status: str, readiness: str = "") -> WorkState:
    status = status.lower()
    if status == "running":
        return WorkState.RUNNING
    if status == "queued":
        return WorkState.READY if readiness.lower() not in {"blocked", "unready", "placeholder", "deferred"} else WorkState.SCHEDULED
    if status == "blocked":
        return WorkState.BLOCKED
    if status == "superseded":
        return WorkState.ARCHIVED
    return WorkState.FINISHED


def _verdict(value: str) -> Verdict | None:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "reject": Verdict.REJECT,
        "revise": Verdict.REVISE,
        "hpo_candidate": Verdict.HPO_CANDIDATE,
        "paper_trade_candidate": Verdict.PAPER_TRADE_CANDIDATE,
        "pass": Verdict.PASS,
        "blocked": Verdict.INFRASTRUCTURE_FAILURE,
        "inconclusive": Verdict.INCONCLUSIVE,
    }
    return aliases.get(normalized)


def _run_status(value: str) -> RunStatus:
    try:
        return RunStatus(value.lower())
    except ValueError:
        return RunStatus.UNKNOWN


def _raw_field(raw: str, name: str) -> str:
    match = re.search(rf"^\s+{re.escape(name)}:\s*(.*?)\s*$", raw, re.MULTILINE)
    return match.group(1).strip().strip("\"'") if match else ""


def _source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class LegacyImporter:
    def __init__(self, repo_root: Path, database: WorkflowDatabase):
        self.repo_root = repo_root
        self.research_root = repo_root / "research"
        self.database = database
        self.counts = {"experiments": 0, "work_items": 0, "evaluations": 0, "runs": 0, "artifacts": 0}

    def import_all(self) -> dict[str, int]:
        self.database.initialize()
        records = legacy_adapter.load_records(self.research_root)
        queue_rows = legacy_adapter.parse_queue(self.research_root / "TEST_JOB_QUEUE.md")
        queue_by_id = {row.job_id: row for row in queue_rows}
        for record in records:
            row = queue_by_id.get(record.job_id)
            raw = row.raw if row else ""
            spec = ExperimentSpec(
                id=record.job_id,
                strategy_name=record.strategy or "unknown",
                experiment_type=_experiment_type(record.job_id, " ".join((record.actual, record.next_step))),
                hypothesis=_raw_field(raw, "hypothesis"),
                archetype=_raw_field(raw, "archetype"),
                target_regime=_raw_field(raw, "target_regime"),
                failure_regime=_raw_field(raw, "failure_regime"),
                source_path=record.experiment_log or "research/TEST_JOB_QUEUE.md",
            )
            self.database.upsert_experiment(spec)
            self.counts["experiments"] += 1
            state = _work_state(record.status, row.readiness if row else "") if row else WorkState.FINISHED
            blocker = _raw_field(raw, "blocker") if row else ""
            dependencies = tuple(legacy_adapter.dependency_ids(row.depends_on)) if row else ()
            self.database.upsert_work_item(WorkItem(
                id=record.job_id,
                experiment_id=record.job_id,
                priority=_priority(record.priority),
                state=state,
                dependencies=dependencies,
                blocker_code="legacy_blocked" if state is WorkState.BLOCKED else None,
                blocker_detail=blocker or (record.next_step if state is WorkState.BLOCKED else None),
                specification={"legacy_rank": record.rank, "legacy_status": record.status},
            ))
            self.counts["work_items"] += 1
            verdict = _verdict(record.verdict)
            if verdict:
                self.database.add_evaluation(Evaluation(
                    experiment_id=record.job_id,
                    verdict=verdict,
                    summary=record.actual,
                    metrics_summary=record.metrics,
                    next_step=record.next_step,
                ))
                self.counts["evaluations"] += 1
            self._artifact(record.job_id, "experiment_report", record.experiment_log)
            self._artifact(record.job_id, "headless_evidence", record.evidence)
        self._import_runs(set(record.job_id for record in records))
        self._record_sources()
        return dict(self.counts)

    def _ensure_experiment(self, job_id: str, strategy: str) -> None:
        with self.database.connect() as connection:
            exists = connection.execute("SELECT 1 FROM experiments WHERE id=?", (job_id,)).fetchone()
        if exists:
            return
        self.database.upsert_experiment(ExperimentSpec(id=job_id, strategy_name=strategy or "unknown", source_path="research/automation/headless_runs"))
        self.database.upsert_work_item(WorkItem(id=job_id, experiment_id=job_id, priority=999, state=WorkState.FINISHED))
        self.counts["experiments"] += 1
        self.counts["work_items"] += 1

    def _import_runs(self, known_ids: set[str]) -> None:
        run_dir = self.research_root / "automation" / "headless_runs"
        for path in sorted(run_dir.glob("*.json")) if run_dir.exists() else []:
            try:
                payload = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            job_id = str(payload.get("job_id") or path.stem)
            strategy = str(payload.get("strategy") or "unknown")
            self._ensure_experiment(job_id, strategy)
            results = payload.get("results") or payload.get("handles") or []
            for index, result in enumerate(results):
                if not isinstance(result, dict):
                    continue
                session_id = str(result.get("session_id") or "")
                route = self._route(result)
                exception = result.get("exception")
                error: dict[str, Any] | None = None
                if exception:
                    error = exception if isinstance(exception, dict) else {"message": str(exception)}
                run = RunResult(
                    id=session_id or f"{job_id}:{index}",
                    experiment_id=job_id,
                    work_item_id=job_id,
                    session_id=session_id,
                    status=_run_status(str(result.get("status") or "unknown")),
                    route=route,
                    dashboard_url=str(result.get("url") or "") or None,
                    metrics=result.get("metrics") if isinstance(result.get("metrics"), dict) else None,
                    error=error,
                )
                self.database.add_run(run, str(path.relative_to(self.repo_root)))
                self.counts["runs"] += 1

    @staticmethod
    def _route(result: dict[str, Any]) -> RouteSpec | None:
        required = ("symbol", "timeframe", "start_date", "finish_date")
        if not all(result.get(key) for key in required):
            return None
        return RouteSpec(
            exchange=str(result.get("exchange") or ""),
            symbol=str(result["symbol"]),
            timeframe=str(result["timeframe"]),
            start_date=str(result["start_date"]),
            finish_date=str(result["finish_date"]),
        )

    def _artifact(self, experiment_id: str, artifact_type: str, path_value: str) -> None:
        if not path_value:
            return
        path = self.repo_root / path_value
        digest = _source_hash(path) if path.exists() and path.is_file() else None
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO artifacts(experiment_id, artifact_type, path, content_hash) VALUES (?, ?, ?, ?)",
                (experiment_id, artifact_type, path_value, digest),
            )
        self.counts["artifacts"] += 1

    def _record_sources(self) -> None:
        sources = [
            self.research_root / "TEST_JOB_QUEUE.md",
            self.research_root / "RESEARCH_JOURNAL.md",
            self.research_root / "automation" / "job_state.json",
        ]
        for path in sources:
            if not path.exists():
                continue
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO migration_sources(path, content_hash, imported_at, record_count) VALUES (?, ?, ?, ?)",
                    (str(path.relative_to(self.repo_root)), _source_hash(path), utc_now(), self.counts["experiments"]),
                )
