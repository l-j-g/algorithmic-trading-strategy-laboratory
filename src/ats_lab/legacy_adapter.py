"""Dependency-free reader for the legacy Markdown/YAML-like research ledger."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QueueRow:
    job_id: str
    rank: str = ""
    priority: str = ""
    status: str = ""
    strategy: str = ""
    next_step: str = ""
    depends_on: str = ""
    readiness: str = ""
    raw: str = ""


@dataclass
class LegacyRecord:
    job_id: str
    rank: str = ""
    status: str = ""
    verdict: str = ""
    strategy: str = ""
    priority: str = ""
    actual: str = ""
    metrics: str = ""
    next_step: str = ""
    experiment_log: str = ""
    evidence: str = ""


def fields(item: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r"^\s*(?:-\s+)?([A-Za-z0-9_]+):\s*(.*?)\s*$", item, re.MULTILINE):
        key, value = match.groups()
        result.setdefault(key, value.strip().strip("\"'"))
    return result


def yaml_items(path: Path, sections: set[str] | None = None) -> list[tuple[str, str]]:
    text = path.read_text() if path.exists() else ""
    result: list[tuple[str, str]] = []
    for match in re.finditer(r"```yaml\s*\n(.*?)```", text, re.DOTALL):
        headings = list(re.finditer(r"^##\s+(.+?)\s*$", text[:match.start()], re.MULTILINE))
        section = headings[-1].group(1) if headings else ""
        if sections is not None and section not in sections:
            continue
        current: list[str] = []
        for line in match.group(1).splitlines() + [""]:
            if line.startswith("- ") and current:
                result.append((section, "\n".join(current)))
                current = [line]
            elif line.startswith("- "):
                current = [line]
            elif current:
                current.append(line)
        if current:
            result.append((section, "\n".join(current)))
    return result


def parse_queue(path: Path) -> list[QueueRow]:
    active_sections = {"Active Queue", "Unblocked Jobs (fresh queue entries)", "Blocked Jobs", "Promotion Track"}
    rows: list[QueueRow] = []
    for _, raw in yaml_items(path, active_sections):
        value = fields(raw)
        if value.get("id") and value.get("status"):
            rows.append(QueueRow(
                job_id=value["id"], rank=value.get("rank", ""), priority=value.get("priority", ""),
                status=value["status"], strategy=value.get("strategy", ""), next_step=value.get("next_step", ""),
                depends_on=value.get("depends_on", ""), readiness=value.get("readiness", ""), raw=raw,
            ))
    return rows


def dependency_ids(value: str) -> list[str]:
    cleaned = value.strip().strip("[]")
    return [part for part in re.split(r"[, ]+", cleaned) if part and part.lower() not in {"none", "null", "-"}]


def load_records(research_root: Path) -> list[LegacyRecord]:
    by_id: dict[str, LegacyRecord] = {}
    for _, raw in yaml_items(research_root / "RESEARCH_JOURNAL.md"):
        value = fields(raw)
        job_id = value.get("id")
        if not job_id:
            continue
        by_id[job_id] = LegacyRecord(
            job_id=job_id, rank=value.get("rank", ""), status=value.get("status", ""),
            verdict=value.get("verdict", ""), strategy=value.get("strategy", ""),
            priority=value.get("priority", ""), actual=value.get("actual", ""),
            metrics=value.get("metrics_text", ""), next_step=value.get("next_action", value.get("next_step", "")),
            experiment_log=value.get("experiment_log", ""),
        )
    for row in parse_queue(research_root / "TEST_JOB_QUEUE.md"):
        record = by_id.setdefault(row.job_id, LegacyRecord(job_id=row.job_id))
        value = fields(row.raw)
        record.rank = record.rank or row.rank
        record.status = row.status or record.status
        record.strategy = record.strategy or row.strategy
        record.priority = record.priority or row.priority
        record.verdict = record.verdict or value.get("verdict", "")
        record.actual = record.actual or value.get("actual", value.get("summary", ""))
        record.metrics = record.metrics or value.get("metrics_text", "")
        record.next_step = record.next_step or row.next_step
        record.experiment_log = record.experiment_log or value.get("experiment_log", "")
    state_path = research_root / "automation" / "job_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    for job_id, detail in state.get("jobs", {}).items():
        record = by_id.setdefault(job_id, LegacyRecord(job_id=job_id))
        record.status = record.status or str(detail.get("status", ""))
        record.strategy = record.strategy or str(detail.get("strategy", ""))
        record.verdict = record.verdict or str(detail.get("verdict", ""))
        record.experiment_log = record.experiment_log or str(detail.get("experiment_log", ""))
    run_dir = research_root / "automation" / "headless_runs"
    for record in by_id.values():
        matches = sorted(run_dir.glob(f"*{record.job_id}*.json")) if run_dir.exists() else []
        record.evidence = str(matches[-1].relative_to(research_root.parent)) if matches else ""
    return sorted(by_id.values(), key=lambda item: (float(item.rank) if _number(item.rank) else 999999, item.job_id))


def _number(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False
