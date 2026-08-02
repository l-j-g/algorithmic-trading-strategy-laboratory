from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ats_lab.audit import build_audit
from ats_lab.database import WorkflowDatabase
from ats_lab.legacy_import import LegacyImporter
from ats_lab.inventory import build_inventory
from ats_lab.contracts import evaluation_from_payload, experiment_from_payload, work_item_from_payload
from ats_lab.models import Evaluation, ExperimentSpec, RunResult, RunStatus, Verdict, WorkItem, WorkState
from ats_lab.reconcile import apply_reconciliation, build_reconciliation, normalize_unattempted_blockers


class WorkflowDatabaseTests(unittest.TestCase):
    def test_operator_control_is_durable_and_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()

            self.assertEqual(database.control_status()["desired_state"], "running")
            paused = database.set_control_state("paused", updated_by="test")

            self.assertEqual(paused["desired_state"], "paused")
            event = database.rows(
                """SELECT event_type,payload_json FROM events
                   WHERE aggregate_id='control'"""
            )[0]
            self.assertEqual(event["event_type"], "control_changed")
            self.assertEqual(json.loads(event["payload_json"])["to"], "paused")

    def test_supervisor_runtime_is_single_current_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()

            database.update_supervisor_runtime(
                worker_id="worker", process_id=123, phase="executing",
                batch_id="BATCH-1", started_at="2026-01-01T00:00:00Z",
                detail={"jobs": 8},
            )
            runtime = database.supervisor_runtime_status()

            self.assertEqual(runtime["phase"], "executing")
            self.assertEqual(runtime["batch_id"], "BATCH-1")
            self.assertEqual(runtime["detail"], {"jobs": 8})

    def test_resolve_blocker_reopens_with_durable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="TestStrategy",
            ))
            database.upsert_work_item(WorkItem(
                id="JOB-1", experiment_id="EXP-1", priority=1,
                state=WorkState.BLOCKED,
                blocker_code="broken_sizing", blocker_detail="too large",
            ))

            item = database.resolve_blocked_work_item(
                "JOB-1",
                resolution_code="sizing_fixed",
                detail="Fee-aware margin cap validated.",
                evidence_ids=["session-1"],
            )

            self.assertEqual(item["state"], "ready")
            self.assertIsNone(item["blocker_code"])
            event = database.rows(
                "SELECT event_type,payload_json FROM events WHERE aggregate_id='JOB-1'"
            )[0]
            payload = json.loads(event["payload_json"])
            self.assertEqual(event["event_type"], "blocker_resolved")
            self.assertEqual(payload["previous_blocker_code"], "broken_sizing")
            self.assertEqual(payload["evidence_ids"], ["session-1"])

            with self.assertRaises(ValueError):
                database.resolve_blocked_work_item(
                    "JOB-1", resolution_code="again", detail="duplicate",
                )

    def test_requeue_finished_evaluation_preserves_run_and_marks_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="TestStrategy",
            ))
            database.upsert_work_item(WorkItem(
                id="JOB-1", experiment_id="EXP-1", priority=1,
                state=WorkState.FINISHED,
            ))
            database.add_run(RunResult(
                id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
                session_id="session-1", status=RunStatus.FINISHED,
                metrics={"route_runs": [{"metrics": {"net_profit": 1}}]},
            ))

            item = database.requeue_finished_evaluation(
                "JOB-1", worker_id="batch-worker",
                reason="recover nested route metrics",
            )

            self.assertEqual(item["state"], "running")
            self.assertEqual(item["claimed_by"], "batch-worker")
            self.assertEqual(item["blocker_code"], "awaiting_batch_evaluation")
            self.assertTrue(item["blocker_detail"].startswith("RECOVERY-"))
            self.assertEqual(
                len(database.pending_batch_evaluation("batch-worker")), 1,
            )
            event = database.rows(
                """SELECT payload_json FROM events
                   WHERE aggregate_id='JOB-1' AND event_type='evaluation_requeued'"""
            )[0]
            self.assertEqual(json.loads(event["payload_json"])["run_id"], "RUN-1")

    def test_claim_is_transactional_and_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(id="EXP-1", strategy_name="TestStrategy"))
            database.upsert_work_item(WorkItem(id="JOB-1", experiment_id="EXP-1", priority=1, state=WorkState.READY))
            claimed = database.claim_next("worker-1")
            self.assertEqual(claimed["id"], "JOB-1")
            self.assertEqual(claimed["state"], "running")
            self.assertIsNone(database.claim_next("worker-2"))

    def test_active_queue_excludes_finished_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            for suffix, state in (("READY", WorkState.READY), ("DONE", WorkState.FINISHED)):
                database.upsert_experiment(ExperimentSpec(id=f"EXP-{suffix}", strategy_name="Test"))
                database.upsert_work_item(WorkItem(id=f"JOB-{suffix}", experiment_id=f"EXP-{suffix}", priority=1, state=state))
            self.assertEqual([row["id"] for row in database.rows("SELECT * FROM active_queue")], ["JOB-READY"])

    def test_transitions_are_idempotent_but_reject_invalid_source_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(id="EXP-1", strategy_name="Test"))
            database.upsert_work_item(WorkItem(id="JOB-1", experiment_id="EXP-1", priority=1, state=WorkState.READY))
            database.claim_next("worker")
            first = database.transition_work_item("JOB-1", WorkState.FINISHED, allowed_from=(WorkState.RUNNING,))
            second = database.transition_work_item("JOB-1", WorkState.FINISHED, allowed_from=(WorkState.RUNNING,))
            self.assertEqual(first["state"], second["state"])
            with self.assertRaises(ValueError):
                database.transition_work_item("JOB-1", WorkState.BLOCKED, allowed_from=(WorkState.RUNNING,))


class ContractTests(unittest.TestCase):
    def test_contracts_parse_typed_values(self) -> None:
        experiment = experiment_from_payload({
            "id": "EXP-1", "strategy_name": "Test", "experiment_type": "baseline",
            "routes": [{"exchange": "Binance", "symbol": "BTC-USDT", "timeframe": "1h", "start_date": "2025-01-01", "finish_date": "2025-12-31"}],
            "success_gates": [{"name": "trades", "operator": ">=", "threshold": 30}], "failure_gates": [],
        })
        work = work_item_from_payload({"id": "JOB-1", "experiment_id": "EXP-1", "state": "ready"})
        evaluation = evaluation_from_payload({"experiment_id": "EXP-1", "verdict": "hpo_candidate"})
        self.assertEqual(experiment.routes[0].symbol, "BTC-USDT")
        self.assertEqual(work.state, WorkState.READY)
        self.assertEqual(evaluation.verdict.value, "hpo_candidate")


class ReconciliationTests(unittest.TestCase):
    def test_normalizes_only_unattempted_blockers_and_preserves_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            for item_id, attempts in (("IDEA", 0), ("FAILED", 1)):
                database.upsert_experiment(ExperimentSpec(id=item_id, strategy_name="Test"))
                database.upsert_work_item(WorkItem(
                    id=item_id, experiment_id=item_id, priority=1, state=WorkState.BLOCKED,
                    attempts=attempts, blocker_code="legacy_blocked", blocker_detail="implementation required",
                ))
            preview = normalize_unattempted_blockers(database)
            self.assertEqual(preview["work_item_ids"], ["IDEA"])
            applied = normalize_unattempted_blockers(database, apply=True)
            self.assertEqual(applied["applied"], ["IDEA"])
            rows = {row["id"]: row for row in database.rows("SELECT * FROM work_items")}
            self.assertEqual(rows["IDEA"]["state"], "scheduled")
            self.assertEqual(rows["FAILED"]["state"], "blocked")
            readiness = json.loads(rows["IDEA"]["specification_json"])["readiness"]
            self.assertEqual(readiness["detail"], "implementation required")

    def test_requirements_pending_items_are_not_promoted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            for item_id, specification in (
                ("PENDING", {"readiness": {"status": "requirements_pending"}}),
                ("RUNNABLE", {}),
            ):
                database.upsert_experiment(ExperimentSpec(id=item_id, strategy_name="Test"))
                database.upsert_work_item(WorkItem(
                    id=item_id,
                    experiment_id=item_id,
                    priority=1,
                    state=WorkState.SCHEDULED,
                    specification=specification,
                ))
            self.assertEqual(database.remaining_chain_count(), 1)
            self.assertIsNotNone(database.reserve_synthesis_cohort(
                worker_id="planner", requested_count=25,
                low_watermark=1, lease_seconds=60,
                retry_cooldown_seconds=0,
            ))
            self.assertEqual(database.promote_scheduled_runnable(3), 1)
            states = {
                row["id"]: row["state"]
                for row in database.rows("SELECT id, state FROM work_items")
            }
            self.assertEqual(states["PENDING"], "scheduled")
            self.assertEqual(states["RUNNABLE"], "ready")

    def test_requirements_pending_ready_item_cannot_be_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(id="PENDING", strategy_name="Test"))
            database.upsert_work_item(WorkItem(
                id="PENDING",
                experiment_id="PENDING",
                priority=1,
                state=WorkState.READY,
                specification={"readiness": {"status": "requirements_pending"}},
            ))
            self.assertIsNone(database.claim_next("worker"))

    def test_classifies_and_applies_conservative_legacy_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            for item_id, state, blocker in (("STALE", WorkState.RUNNING, None),
                                             ("HISTORY", WorkState.BLOCKED, "legacy_blocked"),
                                             ("ACTION", WorkState.BLOCKED, "missing_candles")):
                database.upsert_experiment(ExperimentSpec(id=item_id, strategy_name="Test"))
                database.upsert_work_item(WorkItem(id=item_id, experiment_id=item_id, priority=1,
                                                   state=state, blocker_code=blocker))
            database.add_evaluation(Evaluation(experiment_id="HISTORY", verdict=Verdict.REJECT))
            result = build_reconciliation(database)
            self.assertEqual(result["counts"], {"stale_running": 1, "actionable": 1, "historical_blockers": 1})
            self.assertEqual(database.rows("SELECT state FROM work_items WHERE id='STALE'")[0]["state"], "running")
            changed = apply_reconciliation(database, result)
            self.assertEqual(changed["stale_running_blocked"], ["STALE"])
            states = {row["id"]: row["state"] for row in database.rows("SELECT id, state FROM work_items")}
            self.assertEqual(states, {"ACTION": "blocked", "HISTORY": "archived", "STALE": "blocked"})

    def test_terminal_run_marks_legacy_blocker_as_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(id="OLD", strategy_name="Test"))
            database.upsert_work_item(WorkItem(id="OLD", experiment_id="OLD", priority=1,
                                               state=WorkState.BLOCKED, blocker_code="legacy_blocked"))
            database.add_run(RunResult(id="run-1", experiment_id="OLD", work_item_id="OLD",
                                       session_id="session-1", status=RunStatus.STOPPED))
            result = build_reconciliation(database)
            self.assertEqual([item["id"] for item in result["historical_blockers"]], ["OLD"])


class LegacyImporterTests(unittest.TestCase):
    def make_repo(self, root: Path) -> None:
        research = root / "research"
        (research / "automation" / "headless_runs").mkdir(parents=True)
        (research / "experiments").mkdir()
        (research / "TEST_JOB_QUEUE.md").write_text("""## Active Queue
```yaml
- rank: 1
  id: READY-1
  priority: P0
  status: queued
  readiness: ready
  strategy: ReadyStrategy
  hypothesis: A test hypothesis
  experiment_log: research/experiments/ready.md
```
## Blocked Jobs
```yaml
- rank: 2
  id: DONE-1
  priority: P1
  status: complete
  strategy: DoneStrategy
  verdict: hpo-candidate
  summary: "passed gates"
  metrics_text: "trades=20"
```
""")
        (research / "RESEARCH_JOURNAL.md").write_text("""## Ranked Test Results
```yaml
- rank: 2
  id: DONE-1
  status: complete
  strategy: DoneStrategy
  verdict: hpo-candidate
  actual: "passed gates"
  metrics_text: "trades=20"
```
""")
        (research / "automation" / "job_state.json").write_text('{"jobs": {}}\n')
        (research / "experiments" / "ready.md").write_text("# Ready\n")
        (research / "automation" / "headless_runs" / "DONE-1.json").write_text(json.dumps({
            "job_id": "DONE-1", "strategy": "DoneStrategy", "results": [{
                "session_id": "session-1", "status": "finished", "url": "http://localhost/session-1",
                "symbol": "BTC-USDT", "timeframe": "1h", "start_date": "2025-01-01", "finish_date": "2025-12-31",
                "metrics": {"total": 20, "net_profit_percentage": 5.0},
            }]
        }))

    def test_import_is_idempotent_and_separates_queue_from_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.make_repo(repo)
            database = WorkflowDatabase(repo / "workflow.sqlite3")
            first = LegacyImporter(repo, database).import_all()
            second = LegacyImporter(repo, database).import_all()
            audit = build_audit(database)
            self.assertEqual(first, second)
            self.assertEqual(audit["experiments"], 2)
            self.assertEqual(audit["active_queue"], 1)
            self.assertEqual(audit["verdicts"]["hpo_candidate"], 1)
            self.assertEqual(audit["run_statuses"]["finished"], 1)

    def test_journal_only_blocked_record_is_finished_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self.make_repo(repo)
            journal = repo / "research" / "RESEARCH_JOURNAL.md"
            journal.write_text(journal.read_text() + """
```yaml
- rank: 3
  id: OLD-BLOCKED
  status: blocked
  strategy: OldStrategy
  verdict: blocked
  actual: "infrastructure failed"
```
""")
            database = WorkflowDatabase(repo / "workflow.sqlite3")
            LegacyImporter(repo, database).import_all()
            state = database.rows("SELECT state FROM work_items WHERE id='OLD-BLOCKED'")[0]["state"]
            self.assertEqual(state, "finished")


class InventoryTests(unittest.TestCase):
    def test_classifies_replaced_and_v2_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "algorithmic-trading-strategy-laboratory" / "src" / "ats_lab").mkdir(parents=True)
            (repo / "research").mkdir()
            (repo / "research" / "TEST_JOB_QUEUE.md").write_text("queue")
            (repo / "algorithmic-trading-strategy-laboratory" / "src" / "ats_lab" / "models.py").write_text("models")
            inventory = build_inventory(repo)
            self.assertEqual(inventory["replace"][0]["path"], "research/TEST_JOB_QUEUE.md")
            self.assertEqual(inventory["retain"][0]["path"], "algorithmic-trading-strategy-laboratory/src/ats_lab/models.py")


if __name__ == "__main__":
    unittest.main()
