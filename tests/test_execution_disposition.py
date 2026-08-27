from __future__ import annotations

import unittest

from ats_lab.analysis_input import ExecutionAnalysisInputBuilder
from ats_lab.execution_disposition import (
    ExecutionDispositionPolicy,
    ExecutionRoute,
    FailureKind,
    retry_limit_disposition,
)
from ats_lab.models import Verdict


class ExecutionDispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = ExecutionDispositionPolicy()

    def test_terminal_strategy_failure_routes_to_analysis(self) -> None:
        disposition = self.policy.classify({
            "outcome": "blocked",
            "blocker_code": "jesse_execution_stopped",
            "detail": "qty cannot be 0",
        })

        self.assertIs(disposition.route, ExecutionRoute.ANALYSIS)
        self.assertIs(disposition.kind, FailureKind.STRATEGY_OR_HARNESS)

    def test_infrastructure_wait_retries_without_analysis(self) -> None:
        disposition = self.policy.classify({
            "outcome": "retry",
            "blocker_code": "jesse_execution_deferred",
            "detail": "session remains active",
            "attempt_charged": False,
        })

        self.assertIs(disposition.route, ExecutionRoute.RETRY)
        self.assertIs(disposition.kind, FailureKind.INFRASTRUCTURE)

    def test_blocked_infrastructure_session_routes_to_analysis(self) -> None:
        disposition = self.policy.classify({
            "outcome": "blocked",
            "blocker_code": "jesse_start_recovery_failed",
            "detail": "bounded session recovery exhausted",
            "attempt_charged": True,
        })

        self.assertIs(disposition.route, ExecutionRoute.ANALYSIS)
        self.assertIs(disposition.kind, FailureKind.STRATEGY_OR_HARNESS)

    def test_operator_requirement_remains_explicit(self) -> None:
        disposition = self.policy.classify({
            "outcome": "blocked",
            "blocker_code": "requirements_pending",
            "detail": "validation routes required",
        })

        self.assertIs(disposition.route, ExecutionRoute.OPERATOR)

    def test_legacy_retry_wrapper_recovers_original_failure(self) -> None:
        disposition = retry_limit_disposition({
            "blocker_detail": (
                "jesse_execution_stopped after 5 attempts: qty cannot be 0"
            ),
        })

        self.assertEqual(disposition.code, "jesse_execution_stopped")
        self.assertEqual(disposition.detail, "qty cannot be 0")

    def test_failed_execution_allows_non_promotion_verdicts(self) -> None:
        builder = ExecutionAnalysisInputBuilder()
        row = {"run_status": "stopped"}

        builder.validate_failure_verdict(row, Verdict.REVISE)
        builder.validate_failure_verdict(row, Verdict.REJECT)
        builder.validate_failure_verdict(row, Verdict.INFRASTRUCTURE_FAILURE)
        with self.assertRaisesRegex(ValueError, "infrastructure_failure"):
            builder.validate_failure_verdict(row, Verdict.HPO_CANDIDATE)


if __name__ == "__main__":
    unittest.main()
