from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ats_lab.telemetry_rollup import TelemetryRollup


class TelemetryRollupTests(unittest.TestCase):
    def test_summarizes_task_totals_percentiles_and_alarms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transport.jsonl"
            path.write_text("\n".join([
                json.dumps({
                    "timestamp": "2099-01-01T00:00:00Z",
                    "task_type": "execute_batch",
                    "model_call_count": 1,
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cache_read_tokens": 0,
                    "request_bytes": 100,
                    "response_bytes": 50,
                }),
                json.dumps({
                    "timestamp": "2099-01-01T00:01:00Z",
                    "task_type": "synthesize_batch",
                    "model_call_count": 1,
                    "input_tokens": 20,
                    "output_tokens": 3,
                    "cache_read_tokens": 0,
                    "request_bytes": 130_000,
                    "response_bytes": 60,
                }),
            ]) + "\n", encoding="utf-8")

            result = TelemetryRollup(path).summarize(since_hours=1000000)

            self.assertEqual(
                [item["task_type"] for item in result["task_types"]],
                ["execute_batch", "synthesize_batch"],
            )
            execution = result["task_types"][0]
            self.assertEqual(execution["input_tokens"]["total"], 10)
            self.assertEqual(execution["input_tokens"]["p95"], 10)
            self.assertEqual(
                {alarm["code"] for alarm in result["alarms"]},
                {"execution_model_calls", "synthesis_request_bytes"},
            )

    def test_invalid_lines_and_missing_file_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transport.jsonl"
            path.write_text("not json\n", encoding="utf-8")
            self.assertEqual(
                TelemetryRollup(path).summarize(since_hours=1)["task_types"],
                [],
            )
            self.assertEqual(
                TelemetryRollup(Path(tmp) / "missing.jsonl").summarize(
                    since_hours=1,
                )["task_types"],
                [],
            )


if __name__ == "__main__":
    unittest.main()
