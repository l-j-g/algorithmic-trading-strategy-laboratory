from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.hpo import import_optuna_study
from ats_lab.models import (
    Evaluation,
    ExperimentSpec,
    ExperimentType,
    RunResult,
    RunStatus,
    RouteSpec,
    Verdict,
    WorkItem,
    WorkState,
)
from ats_lab.resources import ResourcePolicy
from ats_lab.supervisor import BatchSupervisor
from ats_lab.worker import DispatchResult


class SequenceDispatcher:
    def __init__(self, results: list[DispatchResult]):
        self.results = results
        self.requests: list[dict] = []

    def dispatch(self, request: dict) -> DispatchResult:
        self.requests.append(request)
        return self.results.pop(0)


class BatchSupervisorTests(unittest.TestCase):
    def test_preflight_gate_runs_before_claim_or_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            before = database.rows(
                "SELECT id,state,attempts FROM work_items ORDER BY id"
            )
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([]), "batch-worker",
                preflight=lambda: {
                    "healthy": False,
                    "blocker_code": "infrastructure_preflight_failed",
                    "failed_check": "memory_api",
                    "detail": "Memory API unavailable at http://127.0.0.1:18000/health",
                },
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "infrastructure_blocked")
            self.assertEqual(result["failed_check"], "memory_api")
            self.assertEqual(before, database.rows(
                "SELECT id,state,attempts FROM work_items ORDER BY id"
            ))

    def test_background_synthesis_does_not_block_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            started = threading.Event()
            release = threading.Event()
            supervisor = BatchSupervisor(
                database,
                SequenceDispatcher([self.execution_result()]),
                "batch-worker",
                resource_policy=ResourcePolicy(
                    execution_batch_size=8, synthesis_low_watermark=75,
                ),
            )
            supervisor._background_synthesis_enabled = True
            supervisor._reserve_cohort = lambda: {
                "id": "COHORT-BACKGROUND",
                "requested_count": 25,
            }

            def fake_synthesize(
                _cohort: dict, *, recovered: int, promoted: int,
                background: bool = False,
            ) -> dict:
                self.assertTrue(background)
                started.set()
                release.wait(timeout=2)
                return {"status": "synthesized"}

            supervisor._synthesize = fake_synthesize
            began = time.perf_counter()
            result = supervisor.run_round()
            elapsed = time.perf_counter() - began

            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(result["status"], "awaiting_analysis_cohort")
            self.assertLess(elapsed, 1.0)
            self.assertEqual(
                supervisor._background_synthesis_detail(),
                {"cohort_id": "COHORT-BACKGROUND", "status": "running"},
            )
            release.set()
            supervisor._synthesis_future.result(timeout=2)
            supervisor._shutdown_background_synthesis()

    def test_background_analysis_does_not_block_next_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            started = threading.Event()
            release = threading.Event()
            supervisor = BatchSupervisor(
                database,
                SequenceDispatcher([self.execution_result(range(1, 5))]),
                "batch-worker",
                resource_policy=ResourcePolicy(
                    execution_batch_size=8, synthesis_low_watermark=0,
                ),
            )
            supervisor._background_analysis_enabled = True

            def fake_analyze(
                _rows: list[dict], *, recovered: int, promoted: int,
                background: bool = False,
            ) -> dict:
                self.assertTrue(background)
                started.set()
                release.wait(timeout=2)
                return {"status": "batch_complete"}

            supervisor._analyze_pending = fake_analyze
            began = time.perf_counter()
            result = supervisor.run_round()
            elapsed = time.perf_counter() - began

            self.assertTrue(started.wait(timeout=1))
            self.assertEqual(result["status"], "analysis_started")
            self.assertLess(elapsed, 1.0)
            self.assertEqual(
                supervisor._background_analysis_detail(),
                {"batch_id": result["batch_id"], "status": "running"},
            )
            release.set()
            supervisor._analysis_future.result(timeout=2)
            supervisor._shutdown_background_analysis()

    def make_database(self, root: str) -> WorkflowDatabase:
        database = WorkflowDatabase(Path(root) / "workflow.sqlite3")
        database.initialize()
        for index in (1, 2):
            database.upsert_experiment(ExperimentSpec(
                id=f"EXP-{index}", strategy_name=f"Strategy{index}",
            ))
            database.upsert_work_item(WorkItem(
                id=f"JOB-{index}", experiment_id=f"EXP-{index}",
                priority=index, state=WorkState.READY,
                specification={"operation": "backtest"},
            ))
        return database

    def make_analysis_database(
        self, root: str, *, job_count: int = 4,
    ) -> WorkflowDatabase:
        database = self.make_database(root)
        for index in range(3, job_count + 1):
            database.upsert_experiment(ExperimentSpec(
                id=f"EXP-{index}", strategy_name=f"Strategy{index}",
            ))
            database.upsert_work_item(WorkItem(
                id=f"JOB-{index}", experiment_id=f"EXP-{index}",
                priority=index, state=WorkState.READY,
                specification={"operation": "backtest"},
            ))
        return database

    @staticmethod
    def execution_result(indices=(1, 2)) -> DispatchResult:
        return DispatchResult(outcome="finished", payload={
            "outcome": "finished",
            "results": [
                {
                    "work_item_id": f"JOB-{index}",
                    "outcome": "finished",
                    "evidence": {"run": {
                        "session_id": f"session-{index}",
                        "status": "finished",
                        "metrics": {"net_profit_percentage": float(index)},
                        "raw_result": {
                            "session_id": f"session-{index}",
                            "status": "finished",
                            "metrics": {"net_profit_percentage": float(index)},
                        },
                    }},
                }
                for index in indices
            ],
        })

    @staticmethod
    def analysis_result(indices=(1, 2)) -> DispatchResult:
        return DispatchResult(outcome="finished", payload={
            "outcome": "finished",
            "evaluations": [
                {
                    "experiment_id": f"EXP-{index}",
                    "verdict": "revise",
                    "finding": f"Result {index} needs more validation.",
                    "next_action": "Run one controlled validation.",
                }
                for index in indices
            ],
            "synthesis_requests": [],
        })

    def test_one_round_uses_separate_execution_and_analysis_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)),
                self.analysis_result(range(1, 5)),
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(
                    execution_batch_size=8, synthesis_low_watermark=0,
                ),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "batch_complete")
            self.assertEqual(
                [request["task_type"] for request in dispatcher.requests],
                ["execute_batch", "analyze_batch"],
            )
            self.assertEqual(
                database.rows("SELECT state,COUNT(*) count FROM work_items GROUP BY state"),
                [{"state": "finished", "count": 4}],
            )
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM runs")[0]["count"], 4,
            )
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM evaluations")[0]["count"], 4,
            )

    def test_analysis_waits_for_minimum_cohort_across_execution_batches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            dispatcher = SequenceDispatcher([
                self.execution_result(),
                self.execution_result(range(3, 5)),
                self.analysis_result(range(1, 5)),
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(
                    execution_batch_size=2, synthesis_low_watermark=0,
                ),
            )

            first = supervisor.run_round()

            self.assertEqual(first["status"], "awaiting_analysis_cohort")
            self.assertEqual(
                [request["task_type"] for request in dispatcher.requests],
                ["execute_batch"],
            )
            self.assertEqual(
                len(database.pending_batch_evaluation("batch-worker")), 2,
            )

            second = supervisor.run_round()

            self.assertEqual(second["status"], "batch_complete")
            self.assertEqual(
                [request["task_type"] for request in dispatcher.requests],
                ["execute_batch", "execute_batch", "analyze_batch"],
            )
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM evaluations")[0]["count"], 4,
            )

    def test_analyzer_receives_bounded_untrusted_memory_hints(self) -> None:
        class Memory:
            def __init__(self) -> None:
                self.queries: list[str] = []

            def deliver(self, _payload: dict) -> None:
                return

            def recall(self, query: str, *, limit: int) -> list[dict]:
                self.queries.append(query)
                return [{
                    "schema_version": 1,
                    "learning_id": "learn-history-1",
                    "experiment_id": "HISTORY-1",
                    "strategy": "HistoryStrategy",
                    "archetype": "trend",
                    "change_scope": "entry",
                    "target_regime": "liquid trend",
                    "failure_regime": "range chop",
                    "lifecycle_stage": "baseline",
                    "verdict": "revise",
                    "reason_codes": ["deterministic_gate_failed"],
                    "normalized_metrics": {},
                    "lesson": "Historical evidence failed in range chop.",
                    "next_refinement_constraint": "Change one entry trigger.",
                    "evaluated_at": "2026-08-01T00:00:00Z",
                }][:limit]

        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            memory = Memory()
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)),
                self.analysis_result(range(1, 5)),
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker", memory_adapter=memory,
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            self.assertEqual(supervisor.run_round()["status"], "batch_complete")

            request = dispatcher.requests[1]
            self.assertEqual(len(request["advisory_memory"]), 1)
            self.assertEqual(
                request["advisory_memory"][0]["trust"],
                "untrusted_advisory_data",
            )
            self.assertFalse(request["memory_degraded"])
            self.assertLessEqual(
                len(json.dumps(request["advisory_memory"]).encode()), 3_200,
            )
            self.assertTrue(any("Strategy1" in query for query in memory.queries))

    def test_infrastructure_retry_does_not_consume_strategy_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            item = database.claim_next("batch-worker")
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([]), "batch-worker",
                retry_delay_seconds=0,
            )

            supervisor._retry_or_block(
                item, "executor_provider_failed", "provider unavailable",
            )

            row = database.rows(
                "SELECT state,attempts,blocker_code FROM work_items WHERE id=?",
                (item["id"],),
            )[0]
            self.assertEqual(row["state"], "waiting_retry")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["blocker_code"], "executor_provider_failed")

    def test_invalid_executor_result_is_infrastructure_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            item = database.claim_next("batch-worker")
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([]), "batch-worker",
                retry_delay_seconds=0,
            )

            supervisor._retry_or_block(
                item, "invalid_executor_result", "Hermes returned malformed JSON",
            )

            row = database.rows(
                "SELECT state,attempts,blocker_code FROM work_items WHERE id=?",
                (item["id"],),
            )[0]

        self.assertEqual(row["state"], "waiting_retry")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["blocker_code"], "invalid_executor_result")

    def test_execution_rejects_metrics_that_do_not_match_raw_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp, job_count=5)
            execution = self.execution_result(range(1, 6))
            execution.payload["results"][0]["evidence"]["run"]["metrics"] = {
                "net_profit_percentage": 999.0,
            }
            analysis = self.analysis_result(range(1, 6))
            analysis.payload["evaluations"] = analysis.payload["evaluations"][1:]
            dispatcher = SequenceDispatcher([execution, analysis])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "batch_complete")
            states = database.rows(
                "SELECT id,state,blocker_code FROM work_items ORDER BY id"
            )
            self.assertEqual(states[0]["state"], "waiting_retry")
            self.assertEqual(states[0]["blocker_code"], "invalid_execution_result")
            self.assertEqual(states[1]["state"], "finished")
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM runs")[0]["count"], 4,
            )

    def test_execution_persists_exact_compact_raw_result_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)),
                self.analysis_result(range(1, 5)),
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            self.assertEqual(supervisor.run_round()["status"], "batch_complete")

            row = database.rows(
                """SELECT metrics_json,raw_result_json FROM runs
                   WHERE session_id='session-1'"""
            )[0]
            metrics = {"net_profit_percentage": 1.0}
            self.assertEqual(json.loads(row["metrics_json"]), metrics)
            self.assertEqual(json.loads(row["raw_result_json"]), {
                "session_id": "session-1",
                "status": "finished",
                "metrics": metrics,
            })

    def test_three_route_aggregate_persists_requested_route_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            routes = [
                {
                    "exchange": "Binance Perpetual Futures",
                    "symbol": symbol,
                    "timeframe": "1h",
                    "start_date": "2025-01-01",
                    "finish_date": "2026-01-01",
                }
                for symbol in ("BTC-USDT", "ETH-USDT", "SOL-USDT")
            ]
            with database.connect() as connection:
                specification = json.loads(connection.execute(
                    "SELECT specification_json FROM experiments WHERE id='EXP-1'"
                ).fetchone()[0])
                specification["routes"] = routes
                connection.execute(
                    "UPDATE experiments SET specification_json=? WHERE id='EXP-1'",
                    (json.dumps(specification),),
                )
            execution = self.execution_result(range(1, 5))
            analysis = self.analysis_result(range(1, 5))
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([execution, analysis]), "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            self.assertEqual(supervisor.run_round()["status"], "batch_complete")

            run = database.rows(
                "SELECT route_json,metrics_json,raw_result_json FROM runs WHERE work_item_id='JOB-1'"
            )[0]
            coverage = json.loads(run["route_json"])
            self.assertEqual(coverage, {
                "coverage": "aggregate_requested_routes",
                "evidence": {
                    "session_id": "session-1",
                    "status": "finished",
                },
                "routes": routes,
            })
            self.assertEqual(
                json.loads(run["metrics_json"]),
                json.loads(run["raw_result_json"])["metrics"],
            )
            evaluation = database.rows(
                "SELECT metrics_summary FROM evaluations WHERE experiment_id='EXP-1'"
            )[0]
            summary = json.loads(evaluation["metrics_summary"])
            self.assertEqual(summary[0].get("symbol"), None)
            self.assertNotIn("failed=route_completion", summary[0]["finding"])

    def test_unfinished_session_does_not_claim_aggregate_route_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            routes = [
                {
                    "exchange": "Binance Perpetual Futures",
                    "symbol": symbol,
                    "timeframe": "1h",
                    "start_date": "2025-01-01",
                    "finish_date": "2026-01-01",
                }
                for symbol in ("BTC-USDT", "ETH-USDT", "SOL-USDT")
            ]
            with database.connect() as connection:
                specification = json.loads(connection.execute(
                    "SELECT specification_json FROM experiments WHERE id='EXP-1'"
                ).fetchone()[0])
                specification["routes"] = routes
                connection.execute(
                    "UPDATE experiments SET specification_json=? WHERE id='EXP-1'",
                    (json.dumps(specification),),
                )
            execution = self.execution_result(range(1, 5))
            run = execution.payload["results"][0]["evidence"]["run"]
            run["status"] = "running"
            run["raw_result"]["status"] = "running"
            analysis = self.analysis_result(range(1, 5))
            dispatcher = SequenceDispatcher([execution, analysis])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            supervisor.run_round()

            persisted = database.rows(
                "SELECT route_json FROM runs WHERE work_item_id='JOB-1'"
            )[0]
            self.assertIsNone(persisted["route_json"])
            evaluation = database.rows(
                """SELECT verdict,metrics_summary FROM evaluations
                   WHERE experiment_id='EXP-1'"""
            )[0]
            summary = json.loads(evaluation["metrics_summary"])
            self.assertEqual(evaluation["verdict"], "revise")
            self.assertNotIn("finding", summary[0])
            analyzed = dispatcher.requests[1]["executions"][0]
            self.assertEqual(analyzed["execution"]["status"], "running")

    def test_partial_retry_persists_terminal_members_and_retries_only_unfinished(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            for index in range(3, 9):
                database.upsert_experiment(ExperimentSpec(
                    id=f"EXP-{index}", strategy_name=f"Strategy{index}",
                ))
                database.upsert_work_item(WorkItem(
                    id=f"JOB-{index}", experiment_id=f"EXP-{index}",
                    priority=index, state=WorkState.READY,
                    specification={"operation": "backtest"},
                ))
            execution = DispatchResult(
                outcome="retry",
                blocker_code="session_running",
                detail="One significance-test session remains running.",
                payload={
                    "outcome": "retry",
                    "results": [
                        {
                            "work_item_id": f"JOB-{index}",
                            "outcome": "finished",
                            "evidence": {"run": {
                                "session_id": f"session-{index}",
                                "status": "finished",
                                "metrics": {"net_profit_percentage": float(index)},
                                "raw_result": {
                                    "session_id": f"session-{index}",
                                    "status": "finished",
                                    "metrics": {"net_profit_percentage": float(index)},
                                },
                            }},
                        }
                        for index in range(1, 8)
                    ] + [{
                        "work_item_id": "JOB-8",
                        "outcome": "retry",
                        "blocker_code": "session_running",
                        "detail": "Significance session still running.",
                    }],
                },
            )
            analysis = DispatchResult(outcome="finished", payload={
                "outcome": "finished",
                "evaluations": [
                    {
                        "experiment_id": f"EXP-{index}",
                        "verdict": "revise",
                        "finding": "Needs validation.",
                        "next_action": "Validate.",
                    }
                    for index in range(1, 8)
                ],
            })
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([execution, analysis]), "batch-worker",
                resource_policy=ResourcePolicy(
                    execution_batch_size=8, synthesis_low_watermark=0,
                ),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "batch_complete")
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM runs")[0]["count"], 7,
            )
            rows = database.rows(
                "SELECT id,state,attempts FROM work_items ORDER BY id"
            )
            completed = [row for row in rows if row["id"] != "JOB-8"]
            unfinished = next(row for row in rows if row["id"] == "JOB-8")
            self.assertEqual({row["state"] for row in completed}, {"finished"})
            self.assertEqual({row["attempts"] for row in completed}, {0})
            self.assertEqual(unfinished["state"], "waiting_retry")
            self.assertEqual(unfinished["attempts"], 1)

    def test_execution_rejects_missing_raw_metrics_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp, job_count=5)
            execution = self.execution_result(range(1, 6))
            del execution.payload["results"][0]["evidence"]["run"][
                "raw_result"
            ]["metrics"]
            analysis = self.analysis_result(range(1, 6))
            analysis.payload["evaluations"] = analysis.payload["evaluations"][1:]
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([execution, analysis]), "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "batch_complete")
            row = database.rows(
                "SELECT state,blocker_code FROM work_items WHERE id='JOB-1'"
            )[0]
            self.assertEqual(row, {
                "state": "waiting_retry",
                "blocker_code": "invalid_execution_result",
            })
            self.assertEqual(
                database.rows(
                    "SELECT COUNT(*) count FROM runs WHERE work_item_id='JOB-1'"
                )[0]["count"],
                0,
            )

    @staticmethod
    def hpo_evidence_metrics() -> dict:
        route = {
            "net_profit_percentage": 10.0, "trade_count": 100, "fees": 20.0,
        }
        windows = (
            ("2024-01-01", "2024-07-01"), ("2024-07-01", "2025-01-01"),
        )
        return {
            "net_profit_percentage": 10.0,
            "fees": 20.0,
            "trade_count": 100,
            "route_results": [
                {
                    "symbol": symbol, "timeframe": "1h",
                    "start_date": start, "finish_date": finish,
                    **route,
                }
                for symbol in ("BTC-USDT", "ETH-USDT")
                for start, finish in windows
            ],
        }

    def test_hpo_candidate_automatically_schedules_hpo_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1-COST2X", strategy_name="Strategy1",
                experiment_type=ExperimentType.COST_SENSITIVITY,
                parent_experiment_id="EXP-1",
            ))
            database.add_run(RunResult(
                id="RUN-COST2X", experiment_id="EXP-1-COST2X",
                work_item_id=None, session_id="session-cost2x",
                status=RunStatus.FINISHED,
                route=RouteSpec(
                    exchange="Binance Perpetual Futures",
                    symbol="SOL-USDT", timeframe="1h",
                    start_date="2024-01-01", finish_date="2025-01-01",
                ),
                metrics={
                    "net_profit_percentage": 8.0,
                    "max_drawdown_percentage": 12.0,
                    "sharpe_ratio": 1.0, "sortino_ratio": 1.0,
                    "calmar_ratio": 1.0, "profit_factor": 1.2,
                    "trade_count": 100, "fees": 40.0,
                },
            ))
            metrics = self.hpo_evidence_metrics()
            execution = self.execution_result(range(1, 5))
            execution.payload["results"][0]["evidence"]["run"]["metrics"] = dict(metrics)
            execution.payload["results"][0]["evidence"]["run"]["raw_result"]["metrics"] = dict(metrics)
            analysis = self.analysis_result(range(1, 5))
            analysis.payload["evaluations"][0]["verdict"] = "hpo_candidate"
            dispatcher = SequenceDispatcher([execution, analysis])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            self.assertEqual(supervisor.run_round()["status"], "batch_complete")

            studies = database.hpo_studies({
                "parent_experiment_id": "EXP-1",
            })
            self.assertEqual(len(studies), 1)
            self.assertEqual(studies[0]["lifecycle_state"], "hpo_scheduled")
            work = database.rows(
                "SELECT state,specification_json FROM work_items WHERE id=?",
                (studies[0]["hpo_work_item_id"],),
            )[0]
            self.assertEqual(work["state"], "scheduled")
            self.assertEqual(
                json.loads(work["specification_json"])["operation"], "hpo",
            )

    def test_paper_trade_claim_enqueues_double_fee_stress_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="Strategy1",
                routes=(RouteSpec(
                    exchange="Binance Perpetual Futures",
                    symbol="BTC-USDT", timeframe="1h",
                    start_date="2024-01-01", finish_date="2025-01-01",
                ),),
            ))
            analysis = self.analysis_result(range(1, 5))
            analysis.payload["evaluations"][0]["verdict"] = (
                "paper_trade_candidate"
            )
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)), analysis,
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            self.assertEqual(supervisor.run_round()["status"], "batch_complete")

            stress_id = "EXP-1-COST2X"
            work = database.rows(
                "SELECT state,specification_json FROM work_items WHERE id=?",
                (stress_id,),
            )
            self.assertEqual(len(work), 1)
            self.assertEqual(work[0]["state"], "scheduled")
            self.assertEqual(
                json.loads(work[0]["specification_json"])["operation"],
                "cost_sensitivity",
            )
            experiment = database.rows(
                "SELECT experiment_type,parent_experiment_id,specification_json "
                "FROM experiments WHERE id=?",
                (stress_id,),
            )[0]
            self.assertEqual(experiment["experiment_type"], "cost_sensitivity")
            self.assertEqual(experiment["parent_experiment_id"], "EXP-1")
            specification = json.loads(experiment["specification_json"])
            self.assertEqual(specification["fee_rate"], 0.001)
            self.assertEqual(len(specification["routes"]), 1)

            repeat = supervisor._enqueue_cost_stress({
                "experiment_id": "EXP-1",
                "experiment_json": experiment["specification_json"],
            })
            self.assertIsNone(repeat)
            self.assertEqual(database.rows(
                "SELECT COUNT(*) count FROM work_items WHERE id=?",
                (stress_id,),
            )[0]["count"], 1)

    def test_hpo_candidate_without_gate_evidence_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            analysis = self.analysis_result(range(1, 5))
            analysis.payload["evaluations"][0]["verdict"] = "hpo_candidate"
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)), analysis,
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            self.assertEqual(supervisor.run_round()["status"], "batch_complete")

            studies = database.hpo_studies({
                "parent_experiment_id": "EXP-1",
            })
            self.assertEqual(studies, [])
            evaluation = database.rows(
                "SELECT verdict,summary FROM evaluations WHERE experiment_id='EXP-1'",
            )[0]
            self.assertEqual(evaluation["verdict"], "inconclusive")
            self.assertIn("HPO-candidate evidence", evaluation["summary"])

    def test_paper_trade_claim_without_validation_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            analysis = self.analysis_result(range(1, 5))
            analysis.payload["evaluations"][0]["verdict"] = (
                "paper_trade_candidate"
            )
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)), analysis,
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            self.assertEqual(supervisor.run_round()["status"], "batch_complete")

            evaluation = database.rows(
                """SELECT verdict,summary,next_step FROM evaluations
                   WHERE experiment_id='EXP-1'"""
            )[0]
            self.assertEqual(evaluation["verdict"], "inconclusive")
            self.assertIn("oos_validation", evaluation["summary"])
            self.assertIn("walk_forward", evaluation["summary"])
            self.assertIn(
                "candles_based_monte_carlo_path_robustness",
                evaluation["summary"],
            )
            self.assertIn("cost-stress", evaluation["next_step"])

    def test_hpo_execution_uses_public_study_id_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="Strategy1",
                routes=(RouteSpec(
                    "Binance Perpetual Futures", "BTC-USDT", "1h",
                    "2025-01-01", "2025-06-01",
                ),),
            ))
            database.transition_work_item(
                "JOB-1", WorkState.FINISHED,
                allowed_from=(WorkState.READY,),
            )
            database.transition_work_item(
                "JOB-2", WorkState.ARCHIVED,
                allowed_from=(WorkState.READY,),
            )
            database.add_evaluation(Evaluation(
                experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
                evaluator="test",
            ))
            study = database.schedule_hpo_candidate("EXP-1", "JOB-1")
            supervisor = BatchSupervisor(
                database,
                SequenceDispatcher([
                    DispatchResult(outcome="retry", detail="temporary"),
                ]),
                "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "execution_failed")
            self.assertEqual(
                database.hpo_study_detail(study["id"])["lifecycle_state"],
                "hpo_running",
            )
            self.assertEqual(
                database.rows(
                    "SELECT state FROM work_items WHERE id=?",
                    (study["hpo_work_item_id"],),
                )[0]["state"],
                "waiting_retry",
            )

    def test_hpo_analysis_payload_is_attempt_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "optuna.sqlite3"
            source_conn = sqlite3.connect(source)
            source_conn.executescript("""
            CREATE TABLE studies(study_id INTEGER PRIMARY KEY,study_name TEXT);
            CREATE TABLE study_directions(
              study_direction_id INTEGER PRIMARY KEY,direction TEXT,
              study_id INTEGER,objective INTEGER
            );
            CREATE TABLE trials(
              trial_id INTEGER PRIMARY KEY,number INTEGER,study_id INTEGER,
              state TEXT,datetime_start TEXT,datetime_complete TEXT
            );
            CREATE TABLE trial_values(
              trial_value_id INTEGER PRIMARY KEY,trial_id INTEGER,
              objective INTEGER,value REAL,value_type TEXT
            );
            CREATE TABLE trial_params(
              param_id INTEGER PRIMARY KEY,trial_id INTEGER,param_name TEXT,
              param_value REAL,distribution_json TEXT
            );
            CREATE TABLE trial_user_attributes(
              trial_user_attribute_id INTEGER PRIMARY KEY,trial_id INTEGER,
              key TEXT,value_json TEXT
            );
            CREATE TABLE trial_system_attributes(
              trial_system_attribute_id INTEGER PRIMARY KEY,trial_id INTEGER,
              key TEXT,value_json TEXT
            );
            INSERT INTO studies VALUES (1,'Trend_optuna');
            INSERT INTO study_directions VALUES (1,'MAXIMIZE',1,0);
            """)
            for number in (7, 8):
                source_conn.execute(
                    """INSERT INTO trials VALUES (
                           ?,?,1,'COMPLETE','2026-01-01 00:00:00',
                           '2026-01-01 00:00:01'
                       )""",
                    (number, number),
                )
                source_conn.execute(
                    "INSERT INTO trial_values VALUES (?,?,0,0.5,'FINITE')",
                    (number, number),
                )
                source_conn.execute(
                    "INSERT INTO trial_params VALUES (?,?, 'period',12,?)",
                    (number, number, json.dumps({"name": "IntDistribution"})),
                )
                source_conn.execute(
                    """INSERT INTO trial_user_attributes VALUES
                           (?,?, 'training_metrics',?)""",
                    (number, number, json.dumps({
                        "net_profit_percentage": 12, "max_drawdown": -4,
                        "sharpe_ratio": 1.5, "total_trades": 40,
                        "sortino_ratio": 1.1, "calmar_ratio": 1.2,
                        "profit_factor": 1.3,
                    })),
                )
                source_conn.execute(
                    """INSERT INTO trial_user_attributes VALUES
                           (?,?, 'testing_metrics',?)""",
                    (number + 10, number, json.dumps({
                        "net_profit_percentage": 8, "max_drawdown": -5,
                        "sharpe_ratio": 1.1, "total_trades": 25,
                        "sortino_ratio": 1.0, "calmar_ratio": 1.1,
                        "profit_factor": 1.2,
                    })),
                )
            source_conn.commit()
            source_conn.close()

            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="EXP-HPO", strategy_name="Trend",
                experiment_type=ExperimentType.HPO,
            ))
            database.upsert_work_item(WorkItem(
                id="JOB-HPO", experiment_id="EXP-HPO", priority=1,
                state=WorkState.FINISHED,
            ))
            result = import_optuna_study(
                database, source, study_name="Trend_optuna",
                parent_experiment_id="EXP-HPO",
                parent_work_item_id="JOB-HPO", strategy="Trend",
            )
            with database.connect() as connection:
                connection.execute(
                    "UPDATE hpo_analysis_jobs SET attempts=2 WHERE study_id=?",
                    (result["study_id"],),
                )
            job = dict(database.rows(
                "SELECT * FROM hpo_analysis_jobs WHERE study_id=?",
                (result["study_id"],),
            )[0])
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([]), "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )
            captured: dict = {}

            def capture(request):
                captured["request"] = request
                return DispatchResult(outcome="finished", payload={
                    "outcome": "finished",
                    "evaluations": [{
                        "experiment_id": (
                            request["executions"][0]["experiment_id"]
                        ),
                        "verdict": "paper_trade_candidate", "finding": "f",
                        "next_action": "n",
                    }],
                })

            supervisor._dispatch = capture

            outcome = supervisor._analyze_hpo_job(job, recovered=0, promoted=0)

            self.assertIn(outcome["status"], {"terminal", "finished"})
            self.assertEqual(outcome["disposition"], "revise")
            detail = database.hpo_study_detail(result["study_id"])
            self.assertEqual(detail["lifecycle_state"], "revise")
            self.assertEqual(
                database.rows(
                    "SELECT verdict FROM evaluations "
                    "WHERE evaluator='ats-lab-hpo-analyzer'"
                )[0]["verdict"],
                "revise",
            )
            evidence = captured["request"]["executions"][0]["evidence"]
            self.assertEqual(len(evidence), 4)

    def test_terminal_hpo_failure_parks_external_trial_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="Strategy1",
                routes=(RouteSpec(
                    "Binance Perpetual Futures", "BTC-USDT", "1h",
                    "2025-01-01", "2025-06-01",
                ),),
            ))
            database.transition_work_item(
                "JOB-1", WorkState.FINISHED,
                allowed_from=(WorkState.READY,),
            )
            database.transition_work_item(
                "JOB-2", WorkState.ARCHIVED,
                allowed_from=(WorkState.READY,),
            )
            database.add_evaluation(Evaluation(
                experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
                evaluator="test",
            ))
            study = database.schedule_hpo_candidate("EXP-1", "JOB-1")
            analysis = DispatchResult(outcome="finished", payload={
                "outcome": "finished",
                "evaluations": [{
                    "experiment_id": study["hpo_experiment_id"],
                    "verdict": "reject",
                    "finding": "Optimizer execution failed before trial evidence.",
                    "next_action": "Import completed optimizer trials.",
                }],
            })
            execution = DispatchResult(outcome="finished", payload={
                "outcome": "finished",
                "results": [{
                    "work_item_id": study["hpo_work_item_id"],
                    "outcome": "blocked",
                    "blocker_code": "executor_timeout",
                    "detail": "Agent execution timed out",
                }],
            })
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([execution, analysis]),
                "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "batch_complete")
            detail = database.hpo_study_detail(study["id"])
            self.assertEqual(detail["lifecycle_state"], "hpo_analysis")
            self.assertEqual(detail["analysis_job"]["state"], "waiting_retry")
            self.assertIn(
                "hpo_trials_required", detail["analysis_job"]["last_error"],
            )
            work = database.rows(
                "SELECT state,blocker_code FROM work_items WHERE id=?",
                (study["hpo_work_item_id"],),
            )[0]
            self.assertEqual(work["state"], "finished")
            self.assertEqual(work["blocker_code"], "hpo_trials_required")

    def test_hpo_execution_without_trials_does_not_loop_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            database.upsert_experiment(ExperimentSpec(
                id="EXP-1", strategy_name="Strategy1",
                routes=(RouteSpec(
                    "Binance Perpetual Futures", "BTC-USDT", "1h",
                    "2025-01-01", "2025-06-01",
                ),),
            ))
            database.transition_work_item(
                "JOB-1", WorkState.FINISHED,
                allowed_from=(WorkState.READY,),
            )
            database.transition_work_item(
                "JOB-2", WorkState.ARCHIVED,
                allowed_from=(WorkState.READY,),
            )
            database.add_evaluation(Evaluation(
                experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
                evaluator="test",
            ))
            study = database.schedule_hpo_candidate("EXP-1", "JOB-1")
            database.configure_hpo_validation_routes(study["id"], {
                "hpo": [{
                    "exchange": "Binance Perpetual Futures",
                    "symbol": "BTC-USDT", "timeframe": "1h",
                    "start_date": "2024-01-01", "finish_date": "2025-01-01",
                }],
            })
            execution = DispatchResult(outcome="finished", payload={
                "outcome": "finished",
                "results": [{
                    "work_item_id": study["hpo_work_item_id"],
                    "outcome": "finished",
                    "evidence": {"run": {
                        "session_id": "hpo-session",
                        "status": "finished",
                        "metrics": {"net_profit_percentage": 1.0},
                        "raw_result": {
                            "session_id": "hpo-session",
                            "status": "finished",
                            "metrics": {"net_profit_percentage": 1.0},
                        },
                    }},
                }],
            })
            dispatcher = SequenceDispatcher([execution])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            first = supervisor.run_round()

            self.assertEqual(first["status"], "batch_terminal")
            self.assertEqual(
                [request["task_type"] for request in dispatcher.requests],
                ["execute_batch"],
            )
            self.assertIsNone(database.claim_hpo_analysis("analyzer"))
            detail = database.hpo_study_detail(study["id"])
            self.assertEqual(detail["lifecycle_state"], "hpo_analysis")
            self.assertEqual(detail["analysis_job"]["state"], "waiting_retry")
            self.assertIn("hpo_trials_required", detail["analysis_job"]["last_error"])

    def test_failed_analysis_retries_once_then_persists_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)),
                DispatchResult(
                    outcome="retry", blocker_code="analyzer_model_failure",
                    detail="model unavailable",
                ),
                DispatchResult(
                    outcome="retry", blocker_code="analyzer_model_failure",
                    detail="model unavailable",
                ),
                DispatchResult(
                    outcome="retry", blocker_code="analyzer_model_failure",
                    detail="model unavailable",
                ),
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "analysis_failed")
            self.assertEqual(len(database.pending_batch_evaluation("batch-worker")), 0)
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM runs")[0]["count"], 4,
            )
            self.assertEqual(
                database.rows(
                    """SELECT COUNT(*) count FROM evaluations
                       WHERE verdict='infrastructure_failure'"""
                )[0]["count"], 4,
            )
            self.assertEqual(
                [request["task_type"] for request in dispatcher.requests],
                [
                    "execute_batch", "analyze_batch",
                    "analyze_batch", "analyze_batch",
                ],
            )
            self.assertEqual(
                database.rows(
                    """SELECT COUNT(*) count FROM work_items
                       WHERE state='blocked'
                         AND blocker_code='analyzer_retry_exhausted'"""
                )[0]["count"], 4,
            )

    def test_invalid_evaluation_batch_retries_then_terminalizes_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            invalid = self.analysis_result(range(1, 5))
            del invalid.payload["evaluations"][1]["next_action"]
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)),
                invalid,
                DispatchResult(
                    outcome="retry", blocker_code="analyzer_model_failure",
                    detail="model unavailable",
                ),
                DispatchResult(
                    outcome="retry", blocker_code="analyzer_model_failure",
                    detail="model unavailable",
                ),
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "analysis_failed")
            self.assertEqual(
                database.rows(
                    """SELECT COUNT(*) count FROM evaluations
                       WHERE verdict='infrastructure_failure'"""
                )[0]["count"], 4,
            )
            self.assertEqual(len(database.pending_batch_evaluation("batch-worker")), 0)

    def test_pause_prevents_new_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            database.set_control_state("paused", updated_by="test")
            dispatcher = SequenceDispatcher([])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "paused")
            self.assertEqual(dispatcher.requests, [])
            self.assertEqual(
                database.rows(
                    "SELECT DISTINCT state FROM work_items ORDER BY state"
                ),
                [{"state": "ready"}],
            )

    def test_stop_request_exits_continuous_loop_without_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            database.set_control_state("stop_requested", updated_by="test")
            dispatcher = SequenceDispatcher([])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run(
                continuous=True, idle_sleep=0, max_rounds=None,
            )

            self.assertEqual(result, [])
            self.assertEqual(dispatcher.requests, [])
            self.assertEqual(
                database.supervisor_runtime_status()["phase"], "stopped",
            )

    def test_stop_request_precedes_pending_batch_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            claimed = database.claim_batch("batch-worker", 8)
            for index, item in enumerate(claimed, 1):
                database.add_run(RunResult(
                    id=f"RUN-{index}",
                    experiment_id=item["experiment_id"],
                    work_item_id=item["id"],
                    session_id=f"session-{index}",
                    status=RunStatus.FINISHED,
                    metrics={"net_profit_percentage": float(index)},
                ))
                database.mark_awaiting_evaluation(item["id"], "BATCH-STOP")
            database.set_control_state("stop_requested", updated_by="test")
            dispatcher = SequenceDispatcher([])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "stop_requested")
            self.assertEqual(dispatcher.requests, [])
            self.assertEqual(
                len(database.pending_batch_evaluation("batch-worker")), 2,
            )

    def test_pause_prevents_pending_batch_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            claimed = database.claim_batch("batch-worker", 8)
            for index, item in enumerate(claimed, 1):
                database.add_run(RunResult(
                    id=f"RUN-{index}",
                    experiment_id=item["experiment_id"],
                    work_item_id=item["id"],
                    session_id=f"session-{index}",
                    status=RunStatus.FINISHED,
                    metrics={"net_profit_percentage": float(index)},
                ))
                database.mark_awaiting_evaluation(item["id"], "BATCH-RECOVERY")
            database.set_control_state("paused", updated_by="test")
            resumed_dispatcher = SequenceDispatcher([self.analysis_result()])
            resumed = BatchSupervisor(
                database, resumed_dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = resumed.run_round()

            self.assertEqual(result["status"], "paused")
            self.assertEqual(resumed_dispatcher.requests, [])
            self.assertEqual(
                database.rows("SELECT state,COUNT(*) count FROM work_items GROUP BY state"),
                [{"state": "running", "count": 2}],
            )

    def test_compact_execution_uses_only_canonical_normalized_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            database.add_run(RunResult(
                id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
                session_id="session-1", status=RunStatus.FINISHED,
                metrics={"route_runs": [{
                    "session_id": "route-1",
                    "route": {
                        "symbol": "BTC-USDT", "timeframe": "1h",
                        "start_date": "2024-01-01", "finish_date": "2025-01-01",
                    },
                    "metrics": {
                        "net_profit_percentage": 12.5,
                        "total_trades": 10,
                        "unneeded": "drop",
                    },
                }]},
            ))
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([]), "batch-worker",
            )

            compact = supervisor._compact_execution({
                "run_id": "RUN-1",
                "work_item_id": "JOB-1",
                "experiment_id": "EXP-1",
                "run_status": "finished",
            })

            self.assertEqual(set(compact), {
                "work_item_id", "experiment_id", "execution", "evidence",
            })
            self.assertEqual(compact["execution"], {"status": "finished"})
            evidence = compact["evidence"][0]
            self.assertEqual(evidence["symbol"], "BTC-USDT")
            self.assertEqual(evidence["net_profit_percentage"], 12.5)
            self.assertEqual(evidence["trade_count"], 10)
            self.assertNotIn("metrics", evidence)
            self.assertNotIn("route_runs", str(compact))
            self.assertNotIn("unneeded", str(compact))

    def test_terminal_strategy_failure_flows_through_analysis_not_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            database.upsert_experiment(ExperimentSpec(
                id="EXP-CHILD", strategy_name="StrategyChild",
            ))
            database.upsert_work_item(WorkItem(
                id="JOB-CHILD", experiment_id="EXP-CHILD", priority=3,
                state=WorkState.SCHEDULED, dependencies=("JOB-1",),
            ))
            execution = self.execution_result(range(1, 5))
            execution.payload["results"][0] = {
                "work_item_id": "JOB-1",
                "outcome": "blocked",
                "blocker_code": "jesse_execution_stopped",
                "detail": "qty cannot be 0",
            }
            analysis = self.analysis_result(range(1, 5))
            dispatcher = SequenceDispatcher([execution, analysis])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            states = {
                row["id"]: row["state"] for row in database.rows(
                    "SELECT id,state FROM work_items"
                )
            }
            failure = dispatcher.requests[1]["executions"][0]
            run = database.rows(
                "SELECT status,error_json FROM runs WHERE work_item_id='JOB-1'"
            )[0]
            self.assertEqual(result["status"], "batch_complete")
            self.assertEqual(states["JOB-1"], "finished")
            self.assertEqual(states["JOB-CHILD"], "archived")
            self.assertEqual(run["status"], "stopped")
            self.assertIn("qty cannot be 0", run["error_json"])
            self.assertEqual(
                failure["execution"]["failure"]["kind"],
                "strategy_or_harness",
            )
            self.assertEqual(failure["evidence"], [])

    def test_terminal_strategy_failures_finalize_without_model_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            execution = self.execution_result()
            for result in execution.payload["results"]:
                result.update({
                    "outcome": "blocked",
                    "blocker_code": "jesse_execution_stopped",
                    "detail": "terminal strategy or harness failure",
                })
            dispatcher = SequenceDispatcher([execution])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "batch_complete")
            self.assertEqual(
                [request["task_type"] for request in dispatcher.requests],
                ["execute_batch"],
            )
            self.assertEqual(
                database.rows(
                    "SELECT verdict,COUNT(*) count FROM evaluations GROUP BY verdict"
                ),
                [{"verdict": "revise", "count": 2}],
            )

    def test_terminal_infrastructure_failure_is_not_strategy_reject(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([]), "batch-worker",
            )
            payload = supervisor._deterministic_failure_analysis_payload({
                "experiment_id": "EXP-1",
                "run_status": "stopped",
                "error_json": json.dumps({
                    "kind": "strategy_or_harness",
                    "code": "malformed_jesse_session",
                    "detail": "session response lacks execution state",
                }),
            })

            self.assertIsNotNone(payload)
            self.assertEqual(payload["verdict"], "infrastructure_failure")
            self.assertIn("not strategy evidence", payload["next_action"])

    def test_analyzer_infrastructure_failure_backs_off_pending_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            dispatcher = SequenceDispatcher([
                self.execution_result(range(1, 5)),
                DispatchResult(
                    outcome="retry",
                    blocker_code="executor_provider_failed",
                    detail="provider unavailable",
                ),
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                retry_delay_seconds=60,
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "analysis_failed")
            self.assertEqual(
                database.pending_batch_evaluation("batch-worker"), [],
            )
            deferred = database.rows(
                """SELECT state,blocker_code,retry_after FROM work_items
                   WHERE state='running' ORDER BY id"""
            )
            self.assertEqual(len(deferred), 4)
            self.assertTrue(all(row["retry_after"] for row in deferred))
            self.assertTrue(all(
                row["blocker_code"] == "awaiting_batch_evaluation"
                for row in deferred
            ))
            self.assertEqual(
                database.rows(
                    """SELECT COUNT(*) count FROM events
                       WHERE event_type='analysis_retry_deferred'"""
                )[0]["count"],
                4,
            )

    def test_legacy_retry_limit_is_recovered_into_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            with database.connect() as connection:
                connection.execute(
                    """UPDATE work_items SET state='blocked',attempts=4,
                       blocker_code='retry_limit_reached',
                       blocker_detail=? WHERE id='JOB-1'""",
                    ("jesse_execution_stopped after 5 attempts: qty cannot be 0",),
                )
                connection.execute(
                    """UPDATE work_items SET state='blocked',
                       blocker_code='missing_exit_framework',
                       blocker_detail='strategy has no explicit exit'
                       WHERE id='JOB-2'"""
                )
            dispatcher = SequenceDispatcher([])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(
                    analysis_cohort_max=8, synthesis_low_watermark=0,
                ),
            )

            result = supervisor.run_round()

            items = database.rows(
                "SELECT id,state,blocker_code FROM work_items ORDER BY id"
            )
            evaluations = database.rows(
                "SELECT experiment_id,verdict FROM evaluations ORDER BY experiment_id"
            )
            self.assertEqual(result["status"], "batch_complete")
            self.assertTrue(all(row["state"] == "finished" for row in items))
            self.assertTrue(all(row["blocker_code"] is None for row in items))
            self.assertEqual(evaluations, [
                {"experiment_id": "EXP-1", "verdict": "revise"},
                {"experiment_id": "EXP-2", "verdict": "revise"},
            ])
            self.assertEqual(dispatcher.requests, [])

    def test_analysis_cohorts_are_balanced_four_to_eight_and_split_hpo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            rows = []
            for index in range(13):
                experiment_type = (
                    ExperimentType.HPO if index == 12
                    else ExperimentType.BASELINE
                )
                experiment = ExperimentSpec(
                    id=f"EXP-{index}", strategy_name=f"Strategy{index}",
                    experiment_type=experiment_type,
                )
                database.upsert_experiment(experiment)
                database.upsert_work_item(WorkItem(
                    id=f"JOB-{index}", experiment_id=experiment.id,
                    priority=index, state=WorkState.RUNNING,
                    specification={
                        "operation": (
                            "hpo" if experiment_type is ExperimentType.HPO
                            else "backtest"
                        ),
                    },
                ))
                rows.append({
                    "work_item_id": f"JOB-{index}",
                    "experiment_id": experiment.id,
                    "experiment_json": json.dumps(experiment.to_dict(), default=str),
                })
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([]), "batch-worker",
                resource_policy=ResourcePolicy(),
            )

            cohorts = supervisor._analysis_cohorts(rows)

            self.assertEqual([len(cohort) for cohort in cohorts], [6, 6, 1])
            self.assertTrue(all(
                supervisor._operation(row) == "hpo" for row in cohorts[-1]
            ))

    def test_compact_payload_is_materially_smaller_than_legacy_raw_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            raw_metrics = {
                "net_profit_percentage": 12.5,
                "total_trades": 100,
                "sharpe_ratio": 1.4,
                "noise": "x" * 50_000,
                "archived_history": [{"text": "y" * 10_000}],
            }
            database.add_run(RunResult(
                id="RUN-SIZE", experiment_id="EXP-1", work_item_id="JOB-1",
                session_id="size-session", status=RunStatus.FINISHED,
                metrics=raw_metrics,
            ))
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([]), "batch-worker",
            )
            row = {
                "run_id": "RUN-SIZE",
                "work_item_id": "JOB-1",
                "experiment_id": "EXP-1",
                "experiment_json": json.dumps({
                    "id": "EXP-1", "strategy_name": "Strategy1",
                    "full_specification": "z" * 20_000,
                }),
            }

            before = len(json.dumps({
                "experiment": json.loads(row["experiment_json"]),
                "run": {"metrics": raw_metrics},
            }).encode())
            after = len(json.dumps(
                supervisor._compact_execution(row),
                separators=(",", ":"),
            ).encode())

            self.assertLess(after, before * 0.1)
            self.assertNotIn("noise", str(supervisor._compact_execution(row)))

    def test_significance_gate_is_applied_before_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            database.upsert_experiment(ExperimentSpec(
                id="SIG-1", strategy_name="Strategy1",
                experiment_type=ExperimentType.SIGNIFICANCE,
            ))
            database.upsert_work_item(WorkItem(
                id="SIG-JOB", experiment_id="SIG-1", priority=1,
                state=WorkState.READY,
                specification={"operation": "significance"},
            ))
            execution = DispatchResult(outcome="finished", payload={
                "outcome": "finished",
                "results": [{
                    "work_item_id": "SIG-JOB",
                    "outcome": "finished",
                    "evidence": {"run": {
                        "session_id": "sig-session",
                        "status": "finished",
                        "raw_result": {
                            "session_id": "sig-session",
                            "status": "finished",
                            "metrics": {"p_value": 0.03},
                        },
                        "metrics": {"p_value": 0.03},
                    }},
                }],
            })
            # Significance verdict is fully determined by canonical p-value;
            # supervisor should persist it without spending an analyzer turn.
            dispatcher = SequenceDispatcher([execution])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            round_result = supervisor.run_round()
            self.assertEqual(round_result["status"], "batch_complete")
            self.assertEqual(
                round_result["cohorts"][0]["analysis_calls_avoided"], 1,
            )
            self.assertEqual(len(dispatcher.requests), 1)
            verdict = database.rows(
                "SELECT verdict FROM evaluations WHERE experiment_id='SIG-1'"
            )[0]["verdict"]
            self.assertEqual(verdict, "pass")

    def test_failed_quality_metrics_skip_agent_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_analysis_database(tmp)
            execution = self.execution_result(range(1, 5))
            for item in execution.payload["results"]:
                metrics = {
                    "net_profit_percentage": -0.37,
                    "sharpe_ratio": -0.03,
                }
                run = item["evidence"]["run"]
                run["metrics"] = metrics
                run["raw_result"]["metrics"] = metrics
            dispatcher = SequenceDispatcher([execution])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "batch_complete")
            self.assertEqual(len(dispatcher.requests), 1)
            self.assertEqual(
                result["cohorts"][0]["analysis_calls_avoided"], 1,
            )
            evaluations = database.rows(
                "SELECT verdict,summary FROM evaluations ORDER BY id"
            )
            self.assertEqual([row["verdict"] for row in evaluations], ["reject"] * 4)
            self.assertTrue(all(
                "Deterministic quality gate: reject." in row["summary"]
                for row in evaluations
            ))

    def test_synthesis_over_generation_is_trimmed_by_lane_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            supervisor = BatchSupervisor(
                self.make_database(tmp), SequenceDispatcher([]), "worker",
            )
            requests = [
                {"lane": "improvement", "id": f"I-{index}"}
                for index in range(20)
            ] + [
                {"lane": "new_concept", "id": f"N-{index}"}
                for index in range(7)
            ]

            bounded = supervisor._bounded_synthesis_requests(requests, 25)

            self.assertEqual(len(bounded), 25)
            self.assertEqual(
                sum(item["lane"] == "improvement" for item in bounded), 20,
            )
            self.assertEqual(
                sum(item["lane"] == "new_concept" for item in bounded), 5,
            )
            with self.assertRaisesRegex(ValueError, "returned 24/25"):
                supervisor._bounded_synthesis_requests(requests[:24], 25)

    def test_synthesis_model_receives_bounded_untrusted_advisory_memory(self) -> None:
        class Memory:
            def deliver(self, _payload: dict) -> None:
                return

            def recall(self, _query: str, *, limit: int) -> list[dict]:
                return [{
                    "schema_version": 1, "learning_id": "learn-1",
                    "experiment_id": "HISTORY-1", "strategy": "HistoryStrategy",
                    "archetype": "trend", "change_scope": "entry",
                    "target_regime": "trend", "failure_regime": "chop",
                    "lifecycle_stage": "baseline", "verdict": "revise",
                    "reason_codes": ["deterministic_gate_failed"],
                    "normalized_metrics": {},
                    "lesson": "Historical evidence failed in chop.",
                    "next_refinement_constraint": "Use one controlled change.",
                    "evaluated_at": "2026-08-01T00:00:00Z",
                }][:limit]

        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            dispatcher = SequenceDispatcher([DispatchResult(
                outcome="retry", detail="stop after context capture",
            )])
            supervisor = BatchSupervisor(
                database, dispatcher, "worker", memory_adapter=Memory(),
            )
            original = database.fail_synthesis_cohort
            database.fail_synthesis_cohort = lambda *_args: None  # type: ignore[method-assign]
            try:
                supervisor._synthesize(
                    {"id": "COHORT-1", "requested_count": 25},
                    recovered=0, promoted=0,
                )
            finally:
                database.fail_synthesis_cohort = original  # type: ignore[method-assign]

            context = dispatcher.requests[0]["context"]
            self.assertFalse(context["memory_degraded"])
            self.assertEqual(len(context["advisory_memory"]), 1)
            self.assertEqual(
                context["advisory_memory"][0]["trust"],
                "untrusted_advisory_data",
            )


if __name__ == "__main__":
    unittest.main()
