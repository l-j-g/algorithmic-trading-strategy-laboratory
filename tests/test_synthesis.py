from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import ExperimentSpec, RouteSpec, RunResult, RunStatus
from ats_lab.synthesis import (
    SynthesisRequest,
    benjamini_hochberg,
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

    def test_benjamini_hochberg_known_family(self) -> None:
        findings = benjamini_hochberg(
            [0.001, 0.008, 0.039, 0.041, 0.2, 0.5], 0.05,
        )
        self.assertEqual([finding["rank"] for finding in findings], [1, 2, 3, 4, 5, 6])
        self.assertEqual(findings[0]["threshold"], 0.05 / 6)
        self.assertAlmostEqual(findings[5]["threshold"], 0.05)
        self.assertEqual(
            [finding["rejected"] for finding in findings],
            [True, True, False, False, False, False],
        )
        with self.assertRaisesRegex(ValueError, "FDR level"):
            benjamini_hochberg([0.01], 0.0)

    def cohort_member(self, slot: int) -> SynthesisRequest:
        return SynthesisRequest(
            strategy_name=f"Cohort{slot}", hypothesis=f"Hypothesis {slot}.",
            entry_rule=f"Entry rule {slot}", change_scope="new_entry",
            routes=(ROUTE,), random_seed=42, cohort_id="COH-1", cohort_slot=slot,
        )

    def finish_cohort_member(
        self, database: WorkflowDatabase, member: dict, p_value: float, slot: int,
    ) -> None:
        database.add_run(RunResult(
            id=f"rst-{member['significance_job']}",
            experiment_id=member["significance_job"],
            work_item_id=member["significance_job"],
            session_id=f"session-{slot}", status=RunStatus.FINISHED,
            metrics={"p_value": p_value},
            finished_at=f"2026-01-0{slot + 1}T00:00:00Z",
        ))

    def test_cohort_significance_gated_by_bh_fdr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            members = [
                synthesize(database, self.cohort_member(slot)) for slot in range(4)
            ]
            for slot, (member, p_value) in enumerate(zip(members, [0.001, 0.008, 0.039])):
                self.finish_cohort_member(database, member, p_value, slot)
                outcome = database.reconcile_significance_gate(
                    member["significance_job"], p_value, active_limit=10,
                )
                self.assertEqual(outcome["decision"], "awaiting_cohort_fdr")
                states = {
                    row["id"]: row["state"]
                    for row in database.rows("SELECT id,state FROM work_items")
                }
                self.assertEqual(states[member["baseline_job"]], "scheduled")

            self.finish_cohort_member(database, members[3], 0.2, 3)
            outcome = database.reconcile_significance_gate(
                members[3]["significance_job"], 0.2, active_limit=10,
            )

            self.assertEqual(outcome["decision"], "cohort_fdr_applied")
            fdr = outcome["cohort_fdr"]
            self.assertEqual(fdr["cohort_id"], "COH-1")
            self.assertEqual(fdr["family_size"], 4)
            self.assertEqual(fdr["fdr_level"], 0.05)
            self.assertEqual(
                [(m["rank"], m["rejected"]) for m in fdr["members"]],
                [(1, True), (2, True), (3, False), (4, False)],
            )
            states = {
                row["id"]: (row["state"], json.loads(row["specification_json"]))
                for row in database.rows(
                    "SELECT id,state,specification_json FROM work_items",
                )
            }
            self.assertEqual(states[members[0]["baseline_job"]][0], "ready")
            self.assertEqual(states[members[1]["baseline_job"]][0], "ready")
            withheld = states[members[2]["baseline_job"]]
            self.assertEqual(withheld[0], "scheduled")
            self.assertEqual(
                withheld[1]["gate_decision"], "significance_withheld_bh_fdr",
            )
            self.assertEqual(withheld[1]["gate_findings"], {
                "procedure": "benjamini_hochberg", "fdr_level": 0.05,
                "family_size": 4, "rank": 3, "threshold": 3 * 0.05 / 4,
                "rejected": False,
            })
            self.assertEqual(states[members[3]["baseline_job"]][0], "archived")


if __name__ == "__main__":
    unittest.main()
