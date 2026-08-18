from __future__ import annotations

import unittest

from ats_lab.strategy_contracts import StrategyContractValidator, max_entry_notional


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

    def test_beta_variant_requires_btc_benchmark_data_route(self) -> None:
        missing = self.validator.validate_request({
            "experiment": {
                "variant": "btc_beta",
                "routes": [{"symbol": "ETH-USDT"}],
            },
        })
        self.assertEqual(
            {issue.code for issue in missing},
            {"missing_beta_benchmark_data_route"},
        )

        present = self.validator.validate_request({
            "experiment": {
                "variant": "btc_beta",
                "routes": [{"symbol": "ETH-USDT"}],
                "data_routes": [{"symbol": "BTC-USDT", "timeframe": "1h"}],
            },
        })
        self.assertEqual(present, ())

    def test_non_beta_variant_does_not_require_benchmark_route(self) -> None:
        self.assertEqual(self.validator.validate_request({
            "experiment": {"variant": "trend", "routes": []},
        }), ())

    def test_liquidation_stress_requires_isolated_mode(self) -> None:
        issues = self.validator.validate_request({
            "experiment": {"liquidation_stress": True, "leverage_mode": "cross"},
        })
        self.assertEqual(
            {issue.code for issue in issues},
            {"liquidation_stress_requires_isolated"},
        )

    def test_isolated_liquidation_stress_is_allowed(self) -> None:
        issues = self.validator.validate_request({
            "experiment": {
                "liquidation_stress": True,
                "futures_leverage_mode": "isolated",
            },
        })
        self.assertEqual(issues, ())

    def test_declared_l_max_uses_session_leverage_sizing_cap(self) -> None:
        self.assertEqual(
            max_entry_notional(10_000, 3, l_max=5),
            28_500,
        )
        with self.assertRaisesRegex(ValueError, "must not exceed declared L_max"):
            max_entry_notional(10_000, 6, l_max=5)

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
