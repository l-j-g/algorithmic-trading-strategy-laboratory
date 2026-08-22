from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ats_lab.database import WorkflowDatabase
from ats_lab.models import ExperimentSpec, ExperimentType, WorkItem, WorkState
from ats_lab.resources import ResourcePolicy
from ats_lab.worker import DispatchResult, Worker


class FakeDispatcher:
    def __init__(self, result: DispatchResult):
        self.result = result
        self.requests: list[dict] = []

    def dispatch(self, request: dict) -> DispatchResult:
        self.requests.append(request)
        return self.result


class WorkerTests(unittest.TestCase):
    def finished(self) -> DispatchResult:
        return DispatchResult(outcome="finished", payload={
            "outcome": "finished",
            "evidence": {
                "run": {"session_id": "session-1", "status": "finished", "metrics": {"net_profit": 1.0}},
                "evaluation": {
                    "verdict": "revise", "summary": "Needs stronger validation.",
                    "metrics_summary": "net_profit=1.0", "next_step": "Run another validation window.",
                    "evaluator": "test",
                },
            },
        })

    def make_database(self, root: str, state: WorkState = WorkState.READY) -> WorkflowDatabase:
        database = WorkflowDatabase(Path(root) / "workflow.sqlite3")
        database.initialize()
        database.upsert_experiment(ExperimentSpec(id="EXP-1", strategy_name="TestStrategy"))
        database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1, state=state,
            specification={"operation": "backtest"},
        ))
        return database

    def synthesis_requests(self) -> list[dict]:
        route = {
            "exchange": "Binance Perpetual Futures", "symbol": "BTC-USDT",
            "timeframe": "1h", "start_date": "2024-01-01", "finish_date": "2025-12-31",
        }
        return [{
            "schema_version": 1, "lane": "new_concept", "action": "new",
            "strategy_name": f"NextStrategy{index}",
            "hypothesis": f"Breakouts continue after compression {index}.",
            "edge_thesis": "Compression can precede persistent expansion.",
            "archetype": "breakout", "target_regime": "volatility expansion",
            "failure_regime": "false breakout",
            "entry_rule": f"Close breaks the twenty-bar high after low ATR {index}",
            "change_scope": "new_entry", "routes": [route],
        } for index in range(25)]

    def test_finished_dispatch_finishes_claimed_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            dispatcher = FakeDispatcher(self.finished())
            result = Worker(database, dispatcher, "worker-1").run_once()
            self.assertEqual(result["status"], "finished")
            self.assertEqual(dispatcher.requests[0]["experiment"]["strategy_name"], "TestStrategy")
            self.assertEqual(dispatcher.requests[0]["work_item"], {"operation": "backtest"})
            self.assertEqual(dispatcher.requests[0]["resource_policy"]["cpu_cores"], 4)
            run = database.rows("SELECT * FROM runs")[0]
            self.assertEqual(run["session_id"], "session-1")
            self.assertEqual(database.rows("SELECT verdict FROM evaluations")[0]["verdict"], "revise")

    def test_experiment_type_requires_evidence_when_operation_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="TestStrategy",
                experiment_type=ExperimentType.BASELINE,
            ))
            database.upsert_work_item(WorkItem(
                id="JOB-1", experiment_id="EXP-1", priority=1,
                state=WorkState.READY,
            ))
            result = Worker(
                database, FakeDispatcher(DispatchResult(outcome="finished", payload={"outcome": "finished"})),
                "worker-1", retry_delay_seconds=1,
            ).run_once()
            self.assertEqual(result["status"], "waiting_retry")
            self.assertEqual(
                database.rows("SELECT blocker_code FROM work_items")[0]["blocker_code"],
                "invalid_run_evidence",
            )

    def test_dispatch_failure_schedules_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            dispatcher = FakeDispatcher(DispatchResult(
                outcome="retry", blocker_code="service_down", detail="try later",
                retry_after="2099-01-01T00:00:00Z",
            ))
            result = Worker(database, dispatcher, "worker-1").run_once()
            row = database.rows("SELECT * FROM work_items WHERE id='JOB-1'")[0]
            self.assertEqual(result["status"], "waiting_retry")
            self.assertEqual(row["attempts"], 1)
            self.assertEqual(row["blocker_code"], "service_down")

    def test_due_retry_is_promoted_then_claimed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, WorkState.WAITING_RETRY)
            past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            with database.connect() as connection:
                connection.execute("UPDATE work_items SET retry_after=? WHERE id='JOB-1'", (past,))
            result = Worker(
                database, FakeDispatcher(DispatchResult(outcome="blocked", detail="manual input")), "worker-1"
            ).run_once()
            self.assertEqual(result["status"], "blocked")

    def test_worker_promotes_scheduled_runnable_work_before_synthesizing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, WorkState.SCHEDULED)
            for index in range(2, 7):
                database.upsert_experiment(ExperimentSpec(id=f"EXP-{index}", strategy_name="Backlog"))
                database.upsert_work_item(WorkItem(
                    id=f"JOB-{index}", experiment_id=f"EXP-{index}",
                    priority=index, state=WorkState.SCHEDULED,
                    specification={"operation": "backtest"},
                ))
            dispatcher = FakeDispatcher(self.finished())
            result = Worker(
                database, dispatcher, "worker-1", synthesize_when_idle=True
            ).run_once()
            self.assertEqual(result["status"], "finished")
            self.assertEqual(dispatcher.requests[0]["work_item_id"], "JOB-1")

    def test_worker_synthesizes_at_five_chain_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, WorkState.SCHEDULED)
            for index in range(2, 6):
                database.upsert_experiment(ExperimentSpec(id=f"EXP-{index}", strategy_name="Backlog"))
                database.upsert_work_item(WorkItem(
                    id=f"JOB-{index}", experiment_id=f"EXP-{index}",
                    priority=index, state=WorkState.SCHEDULED,
                    specification={"operation": "backtest"},
                ))
            dispatcher = FakeDispatcher(DispatchResult(outcome="finished", payload={
                "outcome": "finished",
                "evidence": {"synthesis_requests": self.synthesis_requests()},
            }))
            result = Worker(
                database, dispatcher, "worker-1", synthesize_when_idle=True,
            ).run_once()
            self.assertEqual(result["status"], "synthesized")
            self.assertEqual(database.synthesis_status()["latest_cohort"]["remaining_at_trigger"], 5)

    def test_worker_promotes_dependency_after_parent_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, WorkState.FINISHED)
            database.upsert_experiment(ExperimentSpec(id="EXP-2", strategy_name="Dependent"))
            database.upsert_work_item(WorkItem(
                id="JOB-2", experiment_id="EXP-2", priority=2, state=WorkState.SCHEDULED,
                dependencies=("JOB-1",), specification={"operation": "backtest"},
            ))
            dispatcher = FakeDispatcher(self.finished())
            result = Worker(database, dispatcher, "worker-1").run_once()
            self.assertEqual(result["status"], "finished")
            state = database.rows("SELECT state FROM work_items WHERE id='JOB-2'")[0]["state"]
            self.assertEqual(state, "finished")
            self.assertEqual(dispatcher.requests[0]["work_item_id"], "JOB-2")

    def test_finished_research_without_run_evidence_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            result = Worker(
                database, FakeDispatcher(DispatchResult(outcome="finished", payload={"outcome": "finished"})), "worker-1"
            ).run_once()
            self.assertEqual(result["status"], "waiting_retry")
            row = database.rows("SELECT blocker_code FROM work_items WHERE id='JOB-1'")[0]
            self.assertEqual(row["blocker_code"], "invalid_run_evidence")

    def test_invalid_evaluation_does_not_persist_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            result = self.finished()
            del result.payload["evidence"]["evaluation"]["next_step"]
            outcome = Worker(database, FakeDispatcher(result), "worker-1").run_once()
            self.assertEqual(outcome["status"], "waiting_retry")
            self.assertEqual(database.rows("SELECT COUNT(*) count FROM runs")[0]["count"], 0)
            self.assertEqual(database.rows("SELECT COUNT(*) count FROM evaluations")[0]["count"], 0)

    def test_run_route_ignores_agent_only_strategy_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            result = self.finished()
            result.payload["evidence"]["run"]["route"] = {
                "exchange": "Binance Perpetual Futures", "symbol": "BTC-USDT",
                "timeframe": "1h", "start_date": "2024-01-01", "finish_date": "2024-12-31",
                "strategy": "TestStrategy",
            }
            outcome = Worker(database, FakeDispatcher(result), "worker-1").run_once()
            self.assertEqual(outcome["status"], "finished")
            route = database.rows("SELECT route_json FROM runs")[0]["route_json"]
            self.assertNotIn("strategy", route)

    def test_retry_limit_blocks_instead_of_looping_forever(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            with database.connect() as connection:
                connection.execute("UPDATE work_items SET attempts=4 WHERE id='JOB-1'")
            result = Worker(database, FakeDispatcher(DispatchResult(
                outcome="retry", blocker_code="same_error", detail="still broken",
            )), "worker-1", max_attempts=5).run_once()
            self.assertEqual(result["status"], "blocked")
            row = database.rows("SELECT blocker_code FROM work_items WHERE id='JOB-1'")[0]
            self.assertEqual(row["blocker_code"], "retry_limit_reached")

    def test_retry_receives_prior_failure_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, WorkState.WAITING_RETRY)
            with database.connect() as connection:
                connection.execute("""UPDATE work_items SET attempts=1, retry_after='2000-01-01T00:00:00Z',
                    blocker_code='bad_route', blocker_detail='remove strategy' WHERE id='JOB-1'""")
            dispatcher = FakeDispatcher(self.finished())
            Worker(database, dispatcher, "worker-1").run_once()
            self.assertEqual(dispatcher.requests[0]["prior_failure"]["code"], "bad_route")
            self.assertEqual(dispatcher.requests[0]["prior_failure"]["detail"], "remove strategy")

    def test_worker_restart_recovers_its_abandoned_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, WorkState.RUNNING)
            with database.connect() as connection:
                connection.execute("""UPDATE work_items SET claimed_by='worker-1',
                    claimed_at='2000-01-01T00:00:00Z' WHERE id='JOB-1'""")
            result = Worker(database, FakeDispatcher(self.finished()), "worker-1").run_once()
            self.assertEqual(result["status"], "finished")

    def test_unbounded_continuous_worker_streams_idle_without_accumulating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, WorkState.ARCHIVED)
            streamed = []

            def stop_after_first(_: float) -> None:
                raise KeyboardInterrupt

            worker = Worker(database, FakeDispatcher(self.finished()), "worker-1", sleep=stop_after_first)
            with self.assertRaises(KeyboardInterrupt):
                worker.run(continuous=True, idle_sleep=0, on_result=streamed.append)
            self.assertEqual(streamed, [{"status": "idle", "promoted_retries": 0}])

    def test_idle_worker_synthesizes_next_significance_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp, WorkState.ARCHIVED)
            dispatcher = FakeDispatcher(DispatchResult(outcome="finished", payload={
                "outcome": "finished",
                "evidence": {"synthesis_requests": self.synthesis_requests()},
            }))
            result = Worker(database, dispatcher, "worker-1", synthesize_when_idle=True).run_once()
            self.assertEqual(result["status"], "synthesized")
            ready = database.rows("SELECT id FROM work_items WHERE state='ready'")
            self.assertEqual(len(ready), 3)
            self.assertTrue(all(row["id"].endswith("-SIG") for row in ready))
            self.assertEqual(dispatcher.requests[0]["task_type"], "synthesize_batch")
            self.assertEqual(database.synthesis_status()["latest_cohort"]["generated_count"], 25)

    def test_significance_result_keeps_inconclusive_baseline_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            with database.connect() as connection:
                connection.execute(
                    "UPDATE work_items SET specification_json=? WHERE id='JOB-1'",
                    ('{"operation":"significance"}',),
                )
            database.upsert_experiment(ExperimentSpec(id="EXP-2", strategy_name="Dependent"))
            database.upsert_work_item(WorkItem(
                id="JOB-2", experiment_id="EXP-2", priority=2, state=WorkState.SCHEDULED,
                dependencies=("JOB-1",), specification={"operation": "backtest"},
            ))
            result = self.finished()
            result.payload["evidence"]["run"]["metrics"]["p_value"] = 0.07
            result.payload["evidence"]["evaluation"]["verdict"] = "inconclusive"
            outcome = Worker(database, FakeDispatcher(result), "worker-1").run_once()
            self.assertEqual(outcome["status"], "finished")
            dependent = database.rows("SELECT state,specification_json FROM work_items WHERE id='JOB-2'")[0]
            self.assertEqual(dependent["state"], "scheduled")
            self.assertIn("significance_inconclusive", dependent["specification_json"])

    def test_worker_threads_configured_fdr_level_through(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            with database.connect() as connection:
                connection.execute(
                    "UPDATE work_items SET specification_json=? WHERE id='JOB-1'",
                    ('{"operation":"significance"}',),
                )
            captured: dict[str, float] = {}
            original = WorkflowDatabase.reconcile_significance_gate

            def spy(db, work_item_id, p_value, active_limit, **kwargs):
                captured["fdr_level"] = kwargs.get("fdr_level")
                return original(db, work_item_id, p_value, active_limit, **kwargs)

            result = self.finished()
            result.payload["evidence"]["run"]["metrics"]["p_value"] = 0.03
            result.payload["evidence"]["evaluation"]["verdict"] = "pass"
            with patch.object(
                WorkflowDatabase, "reconcile_significance_gate", spy,
            ):
                outcome = Worker(
                    database, FakeDispatcher(result), "worker-1",
                    resource_policy=ResourcePolicy(significance_fdr_level=0.02),
                ).run_once()

            self.assertEqual(outcome["status"], "finished")
            self.assertEqual(captured["fdr_level"], 0.02)


if __name__ == "__main__":
    unittest.main()
