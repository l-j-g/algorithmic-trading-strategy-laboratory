"""Bounded, privacy-safe Agent transport telemetry summaries."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


NUMERIC_FIELDS = (
    "model_call_count", "input_tokens", "output_tokens",
    "cache_read_tokens", "request_bytes", "response_bytes",
)
MAX_EXECUTION_MODEL_CALLS = 0
MAX_SYNTHESIS_REQUEST_BYTES = 120_000


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class TelemetryRollup:
    path: Path
    max_records: int = 50_000

    def _records(self, since: datetime | None = None) -> Iterable[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ()
        for line in lines[-self.max_records:]:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if since:
                try:
                    observed = datetime.fromisoformat(
                        str(record.get("timestamp", "")).replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if observed < since:
                    continue
            yield {
                "task_type": str(record.get("task_type") or "unknown"),
                **{
                    field: _number(record.get(field))
                    for field in NUMERIC_FIELDS
                },
            }

    def summarize(self, *, since_hours: float = 24) -> dict[str, Any]:
        if since_hours <= 0:
            raise ValueError("since_hours must be positive")
        since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in self._records(since):
            groups.setdefault(record["task_type"], []).append(record)
        summaries = []
        alarms = []
        for task_type, records in sorted(groups.items()):
            item: dict[str, Any] = {"task_type": task_type, "records": len(records)}
            for field in NUMERIC_FIELDS:
                values = [record[field] for record in records if record[field] is not None]
                item[field] = {
                    "total": sum(values) if values else None,
                    "p50": _percentile(values, 0.50),
                    "p95": _percentile(values, 0.95),
                }
            summaries.append(item)
            if task_type == "execute_batch":
                model_calls = sum(
                    record["model_call_count"] or 0 for record in records
                )
                if model_calls > MAX_EXECUTION_MODEL_CALLS:
                    alarms.append({
                        "code": "execution_model_calls",
                        "task_type": task_type,
                        "detail": "direct execution path used model calls",
                        "value": model_calls,
                    })
            if task_type == "synthesize_batch":
                oversized = max(
                    record["request_bytes"] or 0 for record in records
                )
                if oversized > MAX_SYNTHESIS_REQUEST_BYTES:
                    alarms.append({
                        "code": "synthesis_request_bytes",
                        "task_type": task_type,
                        "detail": "synthesis request exceeded bounded context budget",
                        "value": oversized,
                    })
        return {
            "path": str(self.path),
            "since_hours": since_hours,
            "task_types": summaries,
            "alarms": alarms,
        }
