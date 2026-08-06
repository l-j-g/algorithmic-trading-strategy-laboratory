"""Typed routing for executor results and durable failure evidence."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Protocol

from .models import RunResult, RunStatus, utc_now


class ExecutionRoute(StrEnum):
    ANALYSIS = "analysis"
    RETRY = "retry"
    OPERATOR = "operator"


class FailureKind(StrEnum):
    STRATEGY_OR_HARNESS = "strategy_or_harness"
    INFRASTRUCTURE = "infrastructure"
    OPERATOR = "operator"


INFRASTRUCTURE_FAILURE_CODES = frozenset({
    "executor_provider_failed", "executor_timeout", "executor_start_failed",
    "executor_failed", "direct_mcp_error", "malformed_jesse_session",
    "invalid_jesse_metrics", "jesse_execution_deferred",
    "jesse_draft_not_started", "jesse_start_recovery_failed",
    "jesse_zombie_recovery_pending", "jesse_zombie_recovery_required",
    "memory_unavailable", "memory_delivery_failed", "memory_recall_failed",
})

OPERATOR_FAILURE_CODES = frozenset({
    "requirements_pending", "direct_request_changed",
    "credential_required", "operator_input_required",
})

LEGACY_ANALYZABLE_BLOCKERS = frozenset({
    "retry_limit_reached",
    "multi_session_evidence_contract",
    "missing_exit_framework",
    "source_strategy_not_found",
})


@dataclass(frozen=True)
class ExecutionDisposition:
    route: ExecutionRoute
    kind: FailureKind
    code: str
    detail: str


class ExecutionDispositionPolicy:
    """Classify result semantics without performing persistence or I/O."""

    def classify(self, result: Mapping[str, Any]) -> ExecutionDisposition:
        code = str(result.get("blocker_code") or "executor_failure")[:96]
        detail = " ".join(
            str(result.get("detail") or "executor returned no detail").split()
        )[:1000]
        if code in OPERATOR_FAILURE_CODES:
            return ExecutionDisposition(
                ExecutionRoute.OPERATOR, FailureKind.OPERATOR, code, detail,
            )
        if result.get("outcome") == "blocked" or result.get("attempt_charged") is True:
            return ExecutionDisposition(
                ExecutionRoute.ANALYSIS,
                FailureKind.STRATEGY_OR_HARNESS,
                code,
                detail,
            )
        if (
            code in INFRASTRUCTURE_FAILURE_CODES
            or result.get("attempt_charged") is False
        ):
            return ExecutionDisposition(
                ExecutionRoute.RETRY, FailureKind.INFRASTRUCTURE, code, detail,
            )
        return ExecutionDisposition(
            ExecutionRoute.RETRY, FailureKind.INFRASTRUCTURE, code, detail,
        )


class FailureEvidenceDatabase(Protocol):
    def rows(self, query: str, parameters: tuple = ()) -> list[dict]: ...
    def add_failure_run_awaiting_evaluation(
        self, run: RunResult, *, batch_id: str, worker_id: str,
    ) -> None: ...


class ExecutionFailureRecorder:
    """Persist bounded failure evidence and advance work into analysis."""

    def __init__(self, database: FailureEvidenceDatabase, worker_id: str) -> None:
        self.database = database
        self.worker_id = worker_id

    def record(
        self,
        item: Mapping[str, Any],
        disposition: ExecutionDisposition,
        *,
        batch_id: str,
    ) -> str:
        checkpoint = self.database.rows(
            """SELECT session_id FROM direct_execution_sessions
               WHERE work_item_id=?""",
            (item["id"],),
        )
        session_id = str(checkpoint[0]["session_id"]) if checkpoint else None
        identity = json.dumps((
            item["id"], session_id, disposition.code, disposition.detail,
        ), separators=(",", ":"))
        suffix = hashlib.sha256(identity.encode()).hexdigest()[:12].upper()
        run_id = f"{item['id']}:FAILURE:{suffix}"
        finished_at = utc_now()
        run = RunResult(
            id=run_id,
            experiment_id=str(item["experiment_id"]),
            work_item_id=str(item["id"]),
            session_id="",
            status=RunStatus.STOPPED,
            metrics={},
            error={
                "kind": disposition.kind.value,
                "code": disposition.code,
                "detail": disposition.detail,
            },
            finished_at=finished_at,
        )
        self.database.add_failure_run_awaiting_evaluation(
            run, batch_id=batch_id, worker_id=self.worker_id,
        )
        return run_id


def retry_limit_disposition(row: Mapping[str, Any]) -> ExecutionDisposition:
    """Recover original failure semantics from legacy retry-limit detail."""
    raw = " ".join(str(row.get("blocker_detail") or "").split())
    code, marker, remainder = raw.partition(" after ")
    detail = remainder.partition(": ")[2] if marker else raw
    return ExecutionDisposition(
        ExecutionRoute.ANALYSIS,
        FailureKind.STRATEGY_OR_HARNESS,
        (code or "retry_exhausted_execution")[:96],
        (detail or raw or "legacy execution retry exhausted")[:1000],
    )


class TerminalFailureRecovery:
    """Move bounded legacy retry-limit rows into the normal analysis pipeline."""

    def __init__(
        self,
        database: FailureEvidenceDatabase,
        recorder: ExecutionFailureRecorder,
    ) -> None:
        self.database = database
        self.recorder = recorder

    def recover(self, *, batch_id: str, limit: int) -> list[str]:
        blocker_codes = tuple(sorted(LEGACY_ANALYZABLE_BLOCKERS))
        placeholders = ",".join("?" for _ in blocker_codes)
        rows = self.database.rows(
            f"""SELECT id,experiment_id,blocker_code,blocker_detail
               FROM work_items
               WHERE state='blocked'
                 AND blocker_code IN ({placeholders})
               ORDER BY priority,created_at,id LIMIT ?""",
            (*blocker_codes, limit),
        )
        for row in rows:
            disposition = (
                retry_limit_disposition(row)
                if row["blocker_code"] == "retry_limit_reached"
                else ExecutionDisposition(
                    ExecutionRoute.ANALYSIS,
                    FailureKind.STRATEGY_OR_HARNESS,
                    str(row["blocker_code"]),
                    " ".join(str(row.get("blocker_detail") or "").split())[:1000],
                )
            )
            self.recorder.record(
                row, disposition, batch_id=batch_id,
            )
        return [str(row["id"]) for row in rows]
