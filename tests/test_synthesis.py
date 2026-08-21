from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import ExperimentSpec, RouteSpec, RunResult, RunStatus
from ats_lab.synthesis import (
    SynthesisRequest,
    synthesis_request_from_payload,
    synthesize,
)


ROUTE = RouteSpec(
    exchange="Binance Perpetual Futures", symbol="BTC-USDT", timeframe="1h",
    start_date="2024-01-01", finish_date="2025-12-31",
)

ROUTE_PAYLOAD = ROUTE.__dict__


class SynthesisTests(unittest.TestCase):
    def typed_payload(self, proposal_type: str = "new_concept") -> dict:
        payload = {
            "schema_version": 1,
            "type": proposal_type,
            "source_experiment_id": None,
            "controlled_change": "",
            "thesis": "Compression releases into directional expansion.",
            "archetype": "breakout",
            "target_regime": "volatility expansion",
            "failure_regime": "false breakout",
            "falsifiability_criteria": "Reject if edge disappears after costs in out-of-sample candles.",
            "entry_rule_summary": "Close breaks the twenty-bar high after low ATR.",
            "why_this_now": "Recent evidence shows compression is under-tested.",
            "expected_edge_type": "volatility expansion continuation",
            "strategy_name": "TypedBreakout",
            "change_scope": "new_entry",
            "routes": [ROUTE_PAYLOAD],
        }
        if proposal_type == "controlled_improvement":
            payload.update({
                "source_experiment_id": "EXP-1",
                "controlled_change": "Add an ATR expansion confirmation.",
                "change_scope": "entry_changed",
            })
        return payload

    def test_valid_new_concept_typed_proposal(self) -> None:
        request = synthesis_request_from_payload(self.typed_payload())
        self.assertEqual(request.action, "new")
        self.assertEqual(request.lane, "new_concept")
        self.assertIsNone(request.source_experiment_id)
        self.assertEqual(request.hypothesis, "Compression releases into directional expansion.")
        self.assertEqual(request.entry_rule, "Close breaks the twenty-bar high after low ATR.")
        self.assertEqual(request.proposal_type, "new_concept")

    def test_valid_controlled_improvement_typed_proposal(self) -> None:
        request = synthesis_request_from_payload(self.typed_payload("controlled_improvement"))
        self.assertEqual(request.action, "revise")
        self.assertEqual(request.lane, "improvement")
        self.assertEqual(request.source_experiment_id, "EXP-1")
        self.assertEqual(request.controlled_change, "Add an ATR expansion confirmation.")

    def test_malformed_typed_proposal_fails_closed(self) -> None:
        payload = self.typed_payload()
        del payload["falsifiability_criteria"]
        with self.assertRaisesRegex(ValueError, "typed proposal missing fields"):
            synthesis_request_from_payload(payload)

        payload = self.typed_payload()
        payload["type"] = "improvement"
        with self.assertRaisesRegex(ValueError, "unsupported proposal type"):
            synthesis_request_from_payload(payload)

        payload = self.typed_payload("controlled_improvement")
        payload["source_experiment_id"] = None
        with self.assertRaisesRegex(ValueError, "source_experiment_id must be non-empty"):
            synthesis_request_from_payload(payload)

    def test_legacy_manual_payload_remains_compatible(self) -> None:
        request = synthesis_request_from_payload({
            "schema_version": 1,
            "strategy_name": "ManualStrategy",
            "hypothesis": "Manual hypothesis.",
            "entry_rule": "Manual entry rule.",
            "change_scope": "new_entry",
            "routes": [ROUTE_PAYLOAD],
        })
        self.assertEqual(request.action, "new")
        self.assertIsNone(request.proposal_type)

    def make_request(self, scope: str = "new_entry") -> SynthesisRequest:
        return SynthesisRequest(
            strategy_name="TrendPullback", hypothesis="Trend pullbacks continue.",
            entry_rule="EMA trend AND RSI pullback reclaim", change_scope=scope,
            routes=(ROUTE,), random_seed=42,
        )

    def test_new_entry_creates_ready_significance_and_gated_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            result = synthesize(database, self.make_request())
            self.assertEqual(result["decision"], "awaiting_significance")
            states = {row["id"]: row["state"] for row in database.rows("SELECT id,state FROM work_items")}
            self.assertEqual(states[result["significance_job"]], "ready")
            self.assertEqual(states[result["baseline_job"]], "scheduled")
            significance = database.execution_request(result["significance_job"])
            self.assertEqual(significance["experiment"]["routes"], [ROUTE.__dict__])
            self.assertEqual(significance["work_item"]["parameters"]["n_simulations"], 2000)

    def test_passing_significance_unlocks_baseline_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            request = self.make_request()
            first = synthesize(database, request)
            database.add_run(RunResult(
                id="rst-run", experiment_id=first["significance_job"], work_item_id=first["significance_job"],
                session_id="rst-session", status=RunStatus.FINISHED, route=ROUTE,
                metrics={"p_value": 0.03, "n_simulations": 2000}, finished_at="2026-01-01T00:00:00Z",
            ))
            second = synthesize(database, request)
            third = synthesize(database, request)
            self.assertEqual(second["decision"], "significance_passed")
            self.assertEqual(second["baseline_state"], "ready")
            self.assertEqual(second, third)
            self.assertEqual(second["binding_significance_test"], {
                "run_id": "rst-run", "p_value": 0.03,
                "decided_at": "2026-01-01T00:00:00Z",
            })
            self.assertEqual(database.rows("SELECT COUNT(*) count FROM experiments")[0]["count"], 2)

    def revise_request(self) -> SynthesisRequest:
        return SynthesisRequest(
            strategy_name="TrendPullback", hypothesis="Trend pullbacks continue.",
            entry_rule="EMA trend AND RSI pullback reclaim", change_scope="entry_changed",
            routes=(ROUTE,), action="revise", source_experiment_id="EXP-SRC",
            controlled_change="Widen the pullback reclaim window.",
        )

    def test_first_finished_significance_test_is_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-SRC", strategy_name="TrendPullback",
            ))
            request = self.make_request()
            first = synthesize(database, request)
            database.add_run(RunResult(
                id="rst-first", experiment_id=first["significance_job"],
                work_item_id=first["significance_job"], session_id="rst-first",
                status=RunStatus.FINISHED, metrics={"p_value": 0.2},
                finished_at="2026-01-01T00:00:00Z",
            ))
            retest = synthesize(database, self.revise_request())
            self.assertNotEqual(retest["significance_job"], first["significance_job"])
            database.add_run(RunResult(
                id="rst-retest", experiment_id=retest["significance_job"],
                work_item_id=retest["significance_job"], session_id="rst-retest",
                status=RunStatus.FINISHED, metrics={"p_value": 0.01},
                finished_at="2026-01-02T00:00:00Z",
            ))

            result = synthesize(database, request)

            self.assertEqual(result["decision"], "significance_failed")
            self.assertEqual(result["baseline_state"], "archived")
            self.assertEqual(result["binding_significance_test"], {
                "run_id": "rst-first", "p_value": 0.2,
                "decided_at": "2026-01-01T00:00:00Z",
            })

    def test_reconcile_gate_ignores_later_tests_for_same_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-SRC", strategy_name="TrendPullback",
            ))
            first = synthesize(database, self.make_request())
            database.add_run(RunResult(
                id="rst-first", experiment_id=first["significance_job"],
                work_item_id=first["significance_job"], session_id="rst-first",
                status=RunStatus.FINISHED, metrics={"p_value": 0.03},
                finished_at="2026-01-01T00:00:00Z",
            ))
            retest = synthesize(database, self.revise_request())
            database.add_run(RunResult(
                id="rst-retest", experiment_id=retest["significance_job"],
                work_item_id=retest["significance_job"], session_id="rst-retest",
                status=RunStatus.FINISHED, metrics={"p_value": 0.2},
                finished_at="2026-01-02T00:00:00Z",
            ))
            states = {row["id"]: row["state"] for row in database.rows("SELECT id,state FROM work_items")}
            self.assertEqual(states[retest["baseline_job"]], "ready")

            outcome = database.reconcile_significance_gate(
                retest["significance_job"], 0.2, active_limit=4,
            )

            self.assertEqual(
                outcome, {"decision": "superseded_by_first_test", "dependents": []},
            )
            states = {row["id"]: row["state"] for row in database.rows("SELECT id,state FROM work_items")}
            self.assertEqual(states[retest["baseline_job"]], "ready")

    def test_failed_significance_archives_dependent_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            request = self.make_request()
            first = synthesize(database, request)
            database.add_run(RunResult(
                id="rst-run", experiment_id=first["significance_job"], work_item_id=first["significance_job"],
                session_id="rst-session", status=RunStatus.FINISHED, metrics={"p_value": 0.2},
            ))
            result = synthesize(database, request)
            self.assertEqual(result["decision"], "significance_failed")
            self.assertEqual(result["baseline_state"], "archived")

    def test_exit_only_change_skips_significance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            result = synthesize(database, self.make_request("exit_only"))
            self.assertIsNone(result["significance_job"])
            self.assertEqual(result["baseline_state"], "ready")
            self.assertEqual(database.rows("SELECT COUNT(*) count FROM experiments")[0]["count"], 1)


if __name__ == "__main__":
    unittest.main()
