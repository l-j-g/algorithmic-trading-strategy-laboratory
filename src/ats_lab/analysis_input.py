"""Safe analysis contracts for successful and failed executions."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .evidence import NormalizedEvidence
from .models import Verdict


FAILURE_VERDICTS = frozenset({
    Verdict.INFRASTRUCTURE_FAILURE, Verdict.REJECT, Verdict.REVISE,
})


@dataclass(frozen=True)
class ExecutionAnalysisInputBuilder:
    detail_limit: int = 1000

    def build(
        self,
        row: Mapping[str, Any],
        evidence: Sequence[NormalizedEvidence],
    ) -> dict[str, Any]:
        result = {
            "work_item_id": row["work_item_id"],
            "experiment_id": row["experiment_id"],
            "execution": {"status": row.get("run_status") or "unknown"},
            "evidence": [item.to_compact_dict() for item in evidence],
        }
        if row.get("run_status") == "finished":
            return result
        result["evidence"] = []
        try:
            raw_error = json.loads(row.get("error_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_error = {}
        if not isinstance(raw_error, dict):
            raw_error = {}
        result["execution"]["failure"] = {
            "kind": str(raw_error.get("kind") or "strategy_or_harness")[:64],
            "code": str(raw_error.get("code") or "execution_failed")[:96],
            "detail": " ".join(
                str(raw_error.get("detail") or "execution failed").split()
            )[:self.detail_limit],
        }
        return result

    def metrics_summary(
        self,
        row: Mapping[str, Any],
        evidence: Sequence[NormalizedEvidence],
    ) -> list[dict[str, Any]]:
        if row.get("run_status") == "finished":
            return [item.to_compact_dict() for item in evidence]
        execution = self.build(row, evidence)["execution"]
        failure = execution.get("failure") or {}
        return [{
            "execution_status": execution["status"],
            "failure_kind": failure.get("kind"),
            "failure_code": failure.get("code"),
        }]

    @staticmethod
    def validate_failure_verdict(
        row: Mapping[str, Any], verdict: Verdict,
    ) -> None:
        if row.get("run_status") != "finished" and verdict not in FAILURE_VERDICTS:
            raise ValueError(
                "failed execution verdict must be infrastructure_failure, "
                "revise, or reject"
            )
