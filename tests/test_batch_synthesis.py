from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ats_lab.batch_synthesis import ACTIVE_READY_LIMIT, apply_batch, build_batch_context
from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    Evaluation,
    ExperimentSpec,
    RunResult,
    RunStatus,
    Verdict,
    WorkItem,
    WorkState,
)
from ats_lab.resources import ResourcePolicy


ROUTE = {
    "exchange": "Binance Perpetual Futures", "symbol": "BTC-USDT", "timeframe": "1h",
    "start_date": "2024-01-01", "finish_date": "2025-12-31",
}


def request(**overrides):
    payload = {
        "schema_version": 1, "action": "new", "strategy_name": "NewStrategy",
        "hypothesis": "Breakouts continue.", "entry_rule": "Close breaks twenty-bar high",
        "change_scope": "new_entry", "routes": [ROUTE],
    }
    payload.update(overrides)
    return payload


class BatchSynthesisTests(unittest.TestCase):
    def make_database(self, root: str) -> WorkflowDatabase:
        database = WorkflowDatabase(Path(root) / "lab.sqlite3")
        database.initialize()
        return database

    def add_source(self, database: WorkflowDatabase, item_id: str, verdict: Verdict, *, parent: str | None = None) -> None:
        database.upsert_experiment(ExperimentSpec(
            id=item_id, strategy_name=f"Strategy{item_id}", hypothesis="Original hypothesis",
            parent_experiment_id=parent,
        ))
        database.upsert_work_item(WorkItem(
            id=item_id, experiment_id=item_id, priority=10, state=WorkState.SCHEDULED,
        ))
        database.add_evaluation(Evaluation(experiment_id=item_id, verdict=verdict, evaluator="test"))

    def test_context_prioritizes_revise_and_excludes_hpo_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            self.add_source(database, "REVISE", Verdict.REVISE)
            self.add_source(database, "LOCKED", Verdict.HPO_CANDIDATE)
            self.add_source(database, "PLAIN", Verdict.INFRASTRUCTURE_FAILURE)
            context = build_batch_context(database)
            self.assertEqual([row["source_experiment_id"] for row in context["improvement_candidates"]], ["REVISE"])
            self.assertEqual(context["improvement_candidates"][0]["evidence"], [])
            self.assertNotIn(
                "metrics_summary", context["improvement_candidates"][0],
            )
            exposed = {row["source_experiment_id"] for row in context["scheduled_candidates"]}
            self.assertNotIn("LOCKED", exposed)
            self.assertEqual(context["promotion_locked_count"], 1)

    def test_context_is_bounded_to_twenty_five_compact_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            for index in range(25):
                self.add_source(database, f"REVISE-{index}", Verdict.REVISE)
            for index in range(10):
                self.add_source(database, f"REJECT-{index}", Verdict.REJECT)
            for index in range(6):
                database.add_run(RunResult(
                    id=f"RUN-{index}", experiment_id="REVISE-24",
                    work_item_id="REVISE-24", session_id=f"SESSION-{index}",
                    status=RunStatus.FINISHED,
                    metrics={"sharpe_ratio": index / 10},
                ))

            context = build_batch_context(database)
            inspected = (
                len(context["improvement_candidates"])
                + len(context["scheduled_candidates"])
                + len(context["concept_learnings"])
            )

            self.assertEqual(len(context["improvement_candidates"]), 20)
            self.assertLessEqual(inspected, 25)
            candidate = next(
                row for row in context["improvement_candidates"]
                if row["source_experiment_id"] == "REVISE-24"
            )
            self.assertLessEqual(len(candidate["evidence"]), 4)
            self.assertEqual(context["evidence_rows_per_candidate"], 4)

    def test_revise_request_creates_child_but_hpo_source_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            self.add_source(database, "REVISE", Verdict.REVISE)
            self.add_source(database, "LOCKED", Verdict.HPO_CANDIDATE)
            revised = request(
                action="revise", source_experiment_id="REVISE",
                controlled_change="Cap position size to available margin", change_scope="sizing_only",
                strategy_name="StrategyREVISE", entry_rule="Original unchanged entry",
            )
            locked = request(
                action="revise", source_experiment_id="LOCKED",
                controlled_change="Change exit", change_scope="exit_only",
                strategy_name="StrategyLOCKED", entry_rule="Locked entry",
            )
            result = apply_batch(database, [revised, locked])
            self.assertEqual(len(result["generated"]), 1)
            self.assertEqual(result["generated"][0]["source_experiment_id"], "REVISE")
            child = result["generated"][0]["baseline_job"]
            parent = database.rows("SELECT parent_experiment_id FROM experiments WHERE id=?", (child,))[0]
            self.assertEqual(parent["parent_experiment_id"], "REVISE")
            self.assertIn("promotion-locked", result["rejected"][0]["reason"])

    def test_non_entry_revision_uses_canonical_source_entry_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            self.add_source(database, "REVISE", Verdict.REVISE)
            source = json.loads(database.rows(
                "SELECT specification_json FROM experiments WHERE id='REVISE'"
            )[0]["specification_json"])
            description = "Close crosses above canonical EMA gate"
            source["entry_rule"] = {
                "description": description,
                "fingerprint": hashlib.sha256(
                    description.casefold().encode()
                ).hexdigest(),
            }
            with database.connect() as connection:
                connection.execute(
                    """UPDATE experiments SET specification_json=?
                       WHERE id='REVISE'""",
                    (json.dumps(source),),
                )

            result = apply_batch(database, [request(
                action="revise", lane="improvement",
                source_experiment_id="REVISE",
                controlled_change="Reduce risk cap",
                change_scope="risk_only",
                strategy_name="StrategyREVISE",
                entry_rule="Model accidentally changed entry",
            )])

            self.assertEqual(len(result["generated"]), 1)
            child = result["generated"][0]["baseline_job"]
            child_spec = json.loads(database.rows(
                "SELECT specification_json FROM experiments WHERE id=?",
                (child,),
            )[0]["specification_json"])
            self.assertEqual(
                child_spec["entry_rule"]["description"], description,
            )

    def test_ready_capacity_holds_generated_jobs_as_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            for index in range(ACTIVE_READY_LIMIT):
                item_id = f"READY-{index}"
                database.upsert_experiment(ExperimentSpec(id=item_id, strategy_name="Busy"))
                database.upsert_work_item(WorkItem(
                    id=item_id, experiment_id=item_id, priority=1, state=WorkState.READY,
                ))
            result = apply_batch(database, [request()])
            self.assertFalse(result["generated"][0]["released_ready"])
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM work_items WHERE state='ready'")[0]["count"],
                ACTIVE_READY_LIMIT,
            )

    def test_revision_depth_three_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            parent = None
            for item_id in ("ROOT", "CHILD-1", "CHILD-2", "CHILD-3"):
                self.add_source(database, item_id, Verdict.REVISE, parent=parent)
                parent = item_id
            payload = request(
                action="revise", source_experiment_id="CHILD-3", controlled_change="One exit change",
                change_scope="exit_only", strategy_name="StrategyCHILD-3", entry_rule="Same entry",
            )
            result = apply_batch(database, [payload])
            self.assertEqual(result["generated"], [])
            self.assertIn("revision depth limit", result["rejected"][0]["reason"])

    def test_compute_policy_supplies_larger_rst_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            policy = ResourcePolicy(mode="compute_heavy", significance_simulations=5000)
            result = apply_batch(database, [request()], policy=policy)
            sig_id = result["generated"][0]["significance_job"]
            work = database.execution_request(sig_id)["work_item"]
            self.assertEqual(work["parameters"]["n_simulations"], 5000)

    def test_default_batch_accepts_twenty_five_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            payloads = [
                request(
                    strategy_name=f"Strategy{index}",
                    hypothesis=f"Hypothesis {index}",
                    entry_rule=f"Entry rule {index}",
                )
                for index in range(25)
            ]
            result = apply_batch(database, payloads)
            self.assertEqual(len(result["generated"]), 25)
            self.assertEqual(result["rejected"], [])

    def test_cohort_enforces_exact_adaptive_lane_allocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            for index in range(20):
                self.add_source(database, f"REVISE-{index}", Verdict.REVISE)
            with database.connect() as connection:
                connection.execute("UPDATE work_items SET state='archived'")
            payloads = [
                request(
                    action="revise", lane="improvement",
                    source_experiment_id=f"REVISE-{index}",
                    controlled_change=f"Controlled exit change {index}",
                    change_scope="exit_only", strategy_name=f"StrategyREVISE-{index}",
                    entry_rule=f"Stable entry {index}", archetype="trend",
                    target_regime="persistent trend", failure_regime="range",
                    edge_thesis="Trend persistence should survive controlled exit changes.",
                )
                for index in range(20)
            ] + [
                request(
                    lane="new_concept", strategy_name=f"Novel{index}",
                    hypothesis=f"Novel hypothesis {index}", entry_rule=f"Novel entry {index}",
                    archetype="breakout", target_regime="expansion",
                    failure_regime="false breakout",
                    edge_thesis="Compression can precede persistent expansion.",
                )
                for index in range(5)
            ]
            cohort = database.reserve_synthesis_cohort(
                worker_id="planner", requested_count=25, low_watermark=5,
                lease_seconds=60, retry_cooldown_seconds=60,
            )
            result = apply_batch(database, payloads, cohort_id=cohort["id"])
            self.assertEqual(len(result["generated"]), 25)
            self.assertEqual(sum(row["lane"] == "new_concept" for row in result["generated"]), 5)

    def test_single_planner_lease_prevents_duplicate_synthesis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            first = database.reserve_synthesis_cohort(
                worker_id="worker-1", requested_count=25, low_watermark=5,
                lease_seconds=60, retry_cooldown_seconds=60,
            )
            second = database.reserve_synthesis_cohort(
                worker_id="worker-2", requested_count=25, low_watermark=5,
                lease_seconds=60, retry_cooldown_seconds=60,
            )
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            database.fail_synthesis_cohort(first["id"], "invalid response")
            cooldown = database.reserve_synthesis_cohort(
                worker_id="worker-2", requested_count=25, low_watermark=5,
                lease_seconds=60, retry_cooldown_seconds=60,
            )
            self.assertIsNone(cooldown)


if __name__ == "__main__":
    unittest.main()
