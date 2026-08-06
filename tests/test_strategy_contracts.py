from __future__ import annotations

import unittest

from ats_lab.strategy_contracts import StrategyContractValidator


def readiness(**overrides: object) -> dict:
    checks = [
        {"code": code, "status": "pass"}
        for code in (
            "positive_quantity", "exit_shape", "indicator_api", "callback_api",
        )
    ]
    value = {
        "work_item_id": "JOB-1",
        "strategy_name": "TestStrategy",
        "status": "ready",
        "contract_checks": checks,
    }
    value.update(overrides)
    return value


class StrategyContractValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = StrategyContractValidator()

    def test_rejects_explicit_harness_sizing_and_scalar_exits(self) -> None:
        issues = self.validator.validate_request({
            "experiment": {
                "sizing_model": "risk_to_qty from starting balance",
                "entry_rule": {"stop_loss": 0.02, "take_profit": "0.04"},
            },
        })
        self.assertEqual(
            {issue.code for issue in issues},
            {"starting_balance_sizing", "uncapped_risk_sizing", "scalar_exit_target"},
        )

    def test_accepts_capped_sizing_and_sequence_exits(self) -> None:
        issues = self.validator.validate_request({
            "experiment": {
                "sizing_model": "risk_to_qty capped at 95% available_margin",
                "entry_rule": {"stop_loss": [0.5, 1.0], "take_profit": [0.5, 1.2]},
            },
        })
        self.assertEqual(issues, ())

    def test_requires_all_runtime_contract_receipts(self) -> None:
        result = self.validator.validate_readiness(readiness(
            contract_checks=[{"code": "positive_quantity", "status": "pass"}],
        ))
        self.assertTrue(result.malformed)
        self.assertIn("missing required contract checks", result.detail)

    def test_failed_runtime_contract_is_invalid_with_bounded_detail(self) -> None:
        checks = readiness()["contract_checks"]
        checks[2] = {
            "code": "indicator_api", "status": "fail",
            "detail": "self.candles signature mismatch",
        }
        result = self.validator.validate_readiness(readiness(contract_checks=checks))
        self.assertEqual(result.status, "invalid")
        self.assertFalse(result.malformed)
        self.assertIn("indicator_api", result.detail)

    def test_missing_strategy_remains_terminal_readiness(self) -> None:
        result = self.validator.validate_readiness({
            "status": "missing",
            "detail": "class not discoverable",
        })
        self.assertEqual(result.status, "missing")
        self.assertFalse(result.malformed)


if __name__ == "__main__":
    unittest.main()
