from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    ExperimentSpec,
    ExperimentType,
    RunResult,
    RunStatus,
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

    @staticmethod
    def execution_result() -> DispatchResult:
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
                for index in (1, 2)
            ],
        })

    @staticmethod
    def analysis_result() -> DispatchResult:
        return DispatchResult(outcome="finished", payload={
            "outcome": "finished",
            "evaluations": [
                {
                    "experiment_id": f"EXP-{index}",
                    "verdict": "revise",
                    "finding": f"Result {index} needs more validation.",
                    "next_action": "Run one controlled validation.",
                }
                for index in (1, 2)
            ],
            "synthesis_requests": [],
        })

    def test_one_round_uses_separate_execution_and_analysis_turns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            dispatcher = SequenceDispatcher([
                self.execution_result(), self.analysis_result(),
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
                [{"state": "finished", "count": 2}],
            )
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM runs")[0]["count"], 2,
            )
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM evaluations")[0]["count"], 2,
            )

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

    def test_execution_rejects_metrics_that_do_not_match_raw_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            execution = self.execution_result()
            execution.payload["results"][0]["evidence"]["run"]["metrics"] = {
                "net_profit_percentage": 999.0,
            }
            analysis = self.analysis_result()
            analysis.payload["evaluations"] = analysis.payload["evaluations"][1:]
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([execution, analysis]), "batch-worker",
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
                database.rows("SELECT COUNT(*) count FROM runs")[0]["count"], 1,
            )

    def test_execution_persists_exact_compact_raw_result_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            dispatcher = SequenceDispatcher([
                self.execution_result(), self.analysis_result(),
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
            database = self.make_database(tmp)
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
            execution = self.execution_result()
            analysis = self.analysis_result()
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
            database = self.make_database(tmp)
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
            execution = self.execution_result()
            run = execution.payload["results"][0]["evidence"]["run"]
            run["status"] = "running"
            run["raw_result"]["status"] = "running"
            analysis = self.analysis_result()
            supervisor = BatchSupervisor(
                database, SequenceDispatcher([execution, analysis]), "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            supervisor.run_round()

            persisted = database.rows(
                "SELECT route_json FROM runs WHERE work_item_id='JOB-1'"
            )[0]
            self.assertIsNone(persisted["route_json"])
            evaluation = database.rows(
                "SELECT metrics_summary FROM evaluations WHERE experiment_id='EXP-1'"
            )[0]
            summary = json.loads(evaluation["metrics_summary"])
            self.assertIn("failed=route_completion", summary[0]["finding"])

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
            database = self.make_database(tmp)
            execution = self.execution_result()
            del execution.payload["results"][0]["evidence"]["run"][
                "raw_result"
            ]["metrics"]
            analysis = self.analysis_result()
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

    def test_hpo_candidate_automatically_schedules_hpo_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            analysis = self.analysis_result()
            analysis.payload["evaluations"][0]["verdict"] = "hpo_candidate"
            dispatcher = SequenceDispatcher([
                self.execution_result(), analysis,
            ])
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

    def test_failed_analysis_retries_once_then_persists_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            dispatcher = SequenceDispatcher([
                self.execution_result(),
                DispatchResult(outcome="retry", detail="model unavailable"),
            ])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            result = supervisor.run_round()

            self.assertEqual(result["status"], "analysis_failed")
            self.assertEqual(len(database.pending_batch_evaluation("batch-worker")), 0)
            self.assertEqual(
                database.rows("SELECT COUNT(*) count FROM runs")[0]["count"], 2,
            )
            self.assertEqual(
                database.rows(
                    """SELECT COUNT(*) count FROM evaluations
                       WHERE verdict='infrastructure_failure'"""
                )[0]["count"], 2,
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
                )[0]["count"], 2,
            )

    def test_invalid_evaluation_batch_retries_then_terminalizes_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)
            invalid = self.analysis_result()
            del invalid.payload["evaluations"][1]["next_action"]
            dispatcher = SequenceDispatcher([self.execution_result(), invalid])
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
                )[0]["count"], 2,
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

    def test_pause_does_not_abandon_pending_batch_analysis(self) -> None:
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

            self.assertEqual(result["status"], "batch_complete")
            self.assertEqual(
                [request["task_type"] for request in resumed_dispatcher.requests],
                ["analyze_batch"],
            )
            self.assertEqual(
                database.rows("SELECT state,COUNT(*) count FROM work_items GROUP BY state"),
                [{"state": "finished", "count": 2}],
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
            })

            self.assertEqual(set(compact), {
                "work_item_id", "experiment_id", "evidence",
            })
            evidence = compact["evidence"][0]
            self.assertEqual(evidence["symbol"], "BTC-USDT")
            self.assertEqual(evidence["net_profit_percentage"], 12.5)
            self.assertEqual(evidence["trade_count"], 10)
            self.assertNotIn("metrics", evidence)
            self.assertNotIn("route_runs", str(compact))
            self.assertNotIn("unneeded", str(compact))

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
            analysis = DispatchResult(outcome="finished", payload={
                "outcome": "finished",
                "evaluations": [{
                    "experiment_id": "SIG-1",
                    "verdict": "reject",
                    "finding": "Agent verdict should not override gate.",
                    "next_action": "Release dependent baseline.",
                }],
                "synthesis_requests": [],
            })
            dispatcher = SequenceDispatcher([execution, analysis])
            supervisor = BatchSupervisor(
                database, dispatcher, "batch-worker",
                resource_policy=ResourcePolicy(synthesis_low_watermark=0),
            )

            self.assertEqual(supervisor.run_round()["status"], "batch_complete")
            supplied = dispatcher.requests[1]["executions"][0]["evidence"][0]
            self.assertEqual(supplied["significance_p_value"], 0.03)
            self.assertEqual(supplied["verdict"], "pass")
            verdict = database.rows(
                "SELECT verdict FROM evaluations WHERE experiment_id='SIG-1'"
            )[0]["verdict"]
            self.assertEqual(verdict, "pass")

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


if __name__ == "__main__":
    unittest.main()
