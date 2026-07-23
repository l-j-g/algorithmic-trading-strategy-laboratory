from __future__ import annotations

import unittest

from ats_lab.jesse_contracts import jesse_request_from_payload, jesse_result_from_payload


class JesseContractTests(unittest.TestCase):
    def test_request_carries_complete_typed_execution_input(self) -> None:
        request = jesse_request_from_payload({
            "schema_version": 1,
            "transport": "jesse_mcp",
            "request_id": "REQ-1",
            "experiment_id": "EXP-1",
            "work_item_id": "JOB-1",
            "operation": "hpo",
            "strategy_name": "ExampleStrategy",
            "routes": [{"exchange": "Binance", "symbol": "BTC-USDT", "timeframe": "1h", "start_date": "2024-01-01", "finish_date": "2025-01-01"}],
            "parameters": {"fast_period": {"min": 5, "max": 20}},
            "success_gates": [{"name": "sharpe_ratio", "operator": ">=", "threshold": 1.2}],
            "failure_gates": [{"name": "max_drawdown", "operator": ">", "threshold": 25}],
        })
        self.assertEqual(request.operation.value, "hpo")
        self.assertEqual(request.routes[0].start_date, "2024-01-01")
        self.assertEqual(request.parameters["fast_period"]["max"], 20)
        self.assertEqual(request.transport, "jesse_mcp")

    def test_non_mcp_transport_is_rejected(self) -> None:
        payload = {
            "request_id": "REQ-1", "experiment_id": "EXP-1", "work_item_id": "JOB-1",
            "operation": "backtest", "strategy_name": "ExampleStrategy", "transport": "subprocess",
            "routes": [{"exchange": "Binance", "symbol": "BTC-USDT", "timeframe": "1h", "start_date": "2024-01-01", "finish_date": "2025-01-01"}],
        }
        with self.assertRaisesRegex(ValueError, "must be jesse_mcp"):
            jesse_request_from_payload(payload)

    def test_finished_result_preserves_session_dashboard_and_metrics(self) -> None:
        result = jesse_result_from_payload({
            "request_id": "REQ-1", "experiment_id": "EXP-1", "work_item_id": "JOB-1",
            "status": "finished", "session_id": "session-1",
            "dashboard_url": "http://127.0.0.1:9000/#/backtest/session-1",
            "metrics": {"sharpe_ratio": 1.4},
        })
        self.assertEqual(result.session_id, "session-1")
        self.assertEqual(result.metrics["sharpe_ratio"], 1.4)

    def test_stopped_result_requires_structured_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "error is required"):
            jesse_result_from_payload({
                "request_id": "REQ-1", "experiment_id": "EXP-1", "work_item_id": "JOB-1",
                "status": "stopped", "session_id": "session-1", "metrics": {},
            })


if __name__ == "__main__":
    unittest.main()
