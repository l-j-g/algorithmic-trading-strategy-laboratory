from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ats_lab.correctness_recovery import (
    backfill_aggregate_route_coverage,
    classify_recovery_candidates,
    recover_executor_infrastructure_failures,
    recover_partial_batch_retries,
    recover_zombie_execution_sessions,
    recover_verified_margin_sizing_blocker,
    recover_unexecuted_draft_checkpoint,
)
from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    Evaluation,
    ExperimentSpec,
    ExperimentType,
    RouteSpec,
    RunResult,
    RunStatus,
    Verdict,
    WorkItem,
    WorkState,
)


class CorrectnessRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = WorkflowDatabase(Path(self.temp.name) / "lab.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_backfill_requires_finished_session_evidence_and_preserves_raw_metrics(self) -> None:
        routes = tuple(
            RouteSpec(
                exchange="Binance Perpetual Futures", symbol=symbol,
                timeframe="1h", start_date="2025-01-01",
                finish_date="2026-01-01",
            )
            for symbol in ("BTC-USDT", "ETH-USDT", "SOL-USDT")
        )
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="Trend",
            experiment_type=ExperimentType.BASELINE, routes=routes,
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1,
            state=WorkState.FINISHED,
        ))
        metrics = {
            "net_profit_percentage": 12.0, "total": 100,
            "max_drawdown": -10.0, "sharpe_ratio": 1.2,
            "gross_profit": 140.0, "gross_loss": -100.0, "fee": 1.0,
        }
        raw = {
            "session_id": "session-1", "status": "finished",
            "metrics": metrics,
        }
        self.database.add_run(RunResult(
            id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="session-1", status=RunStatus.FINISHED,
            metrics=metrics, raw_result=raw,
        ))
        self.database.add_evaluation(Evaluation(
            experiment_id="EXP-1", verdict=Verdict.REJECT,
            summary="Baseline failed route completion despite positive metrics.",
            next_step="Repair route evidence.", evaluator="ats-lab-batch-analyzer",
        ))
        before = self.database.rows(
            "SELECT metrics_json,raw_result_json FROM runs WHERE id='RUN-1'"
        )[0]

        preview = backfill_aggregate_route_coverage(self.database, apply=False)
        applied = backfill_aggregate_route_coverage(self.database, apply=True)

        self.assertEqual(preview["eligible"], ["RUN-1"])
        self.assertEqual(applied["updated"], ["RUN-1"])
        after = self.database.rows(
            "SELECT route_json,metrics_json,raw_result_json FROM runs WHERE id='RUN-1'"
        )[0]
        self.assertEqual(after["metrics_json"], before["metrics_json"])
        self.assertEqual(after["raw_result_json"], before["raw_result_json"])
        self.assertEqual(
            len(json.loads(after["route_json"])["routes"]), 3,
        )
        evaluation = self.database.rows(
            "SELECT verdict,metrics_summary FROM evaluations WHERE experiment_id='EXP-1'"
        )[0]
        self.assertEqual(evaluation["verdict"], "inconclusive")
        self.assertIn("missing=fees_cost_sensitivity", evaluation["metrics_summary"])
        self.assertNotIn(
            "failed=route_completion", evaluation["metrics_summary"],
        )

    def test_backfill_skips_missing_actual_session_evidence(self) -> None:
        routes = (
            RouteSpec("Binance", "BTC-USDT", "1h", "2025-01-01", "2026-01-01"),
            RouteSpec("Binance", "ETH-USDT", "1h", "2025-01-01", "2026-01-01"),
        )
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="Trend",
            experiment_type=ExperimentType.BASELINE, routes=routes,
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1,
            state=WorkState.FINISHED,
        ))
        self.database.add_run(RunResult(
            id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="session-1", status=RunStatus.FINISHED,
            metrics={"net_profit_percentage": 1.0},
        ))

        result = backfill_aggregate_route_coverage(self.database, apply=True)

        self.assertEqual(result["updated"], [])
        self.assertEqual(result["skipped_missing_session_evidence"], ["RUN-1"])

    def test_recovery_only_reopens_explicit_matching_batch_bug_ids(self) -> None:
        for item_id, blocker_code, detail in (
            (
                "A", "retry_limit_reached",
                "batch_execution_failed after 5 attempts: "
                "One significance-test session remains running; seven work items finished.",
            ),
            ("B", "source_strategy_not_found", "missing"),
        ):
            self.database.upsert_experiment(ExperimentSpec(
                id=item_id, strategy_name=item_id,
            ))
            self.database.upsert_work_item(WorkItem(
                id=item_id, experiment_id=item_id, priority=1,
                state=WorkState.BLOCKED, attempts=4,
                blocker_code=blocker_code, blocker_detail=detail,
            ))

        preview = recover_partial_batch_retries(
            self.database, ["A", "B"], apply=False,
        )
        applied = recover_partial_batch_retries(
            self.database, ["A", "B"], apply=True,
        )

        self.assertEqual(preview["eligible"], ["A"])
        self.assertEqual(applied["recovered"], ["A"])
        rows = {
            row["id"]: row for row in self.database.rows(
                "SELECT id,state,attempts,blocker_code FROM work_items"
            )
        }
        self.assertEqual(rows["A"], {
            "id": "A", "state": "ready", "attempts": 0,
            "blocker_code": None,
        })
        self.assertEqual(rows["B"]["state"], "blocked")
        self.assertEqual(rows["B"]["blocker_code"], "source_strategy_not_found")

    def test_executor_recovery_replays_persisted_evidence_without_execution(self) -> None:
        self.database.upsert_experiment(ExperimentSpec(
            id="SIG-1", strategy_name="Trend",
            experiment_type=ExperimentType.SIGNIFICANCE,
        ))
        self.database.upsert_work_item(WorkItem(
            id="SIG-1", experiment_id="SIG-1", priority=1,
            state=WorkState.BLOCKED, blocker_code="analyzer_retry_exhausted",
            blocker_detail="Analyzer failed after 2 attempts: Expecting value",
        ))
        metrics = {
            "p_value": 0.03, "observed_mean": 1.0,
            "annualized_return": 2.0, "n_simulations": 2000,
            "n_observations": 100,
        }
        self.database.add_run(RunResult(
            id="RUN-1", experiment_id="SIG-1", work_item_id="SIG-1",
            session_id="session-1", status=RunStatus.FINISHED,
            metrics=metrics, raw_result={
                "session_id": "session-1", "status": "finished",
                "metrics": metrics,
            },
        ))

        result = recover_executor_infrastructure_failures(
            self.database, apply=True, worker_id="recovery-worker",
        )
        again = recover_executor_infrastructure_failures(
            self.database, apply=True, worker_id="recovery-worker",
        )

        self.assertEqual(result["evidence_replay"], ["SIG-1"])
        self.assertEqual(again["changed"], [])
        row = self.database.rows(
            "SELECT state,attempts,blocker_code FROM work_items WHERE id='SIG-1'"
        )[0]
        self.assertEqual(row["state"], "running")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["blocker_code"], "awaiting_batch_evaluation")
        self.assertEqual(
            self.database.rows("SELECT COUNT(*) n FROM runs")[0]["n"], 1,
        )

    def test_recover_one_proven_unexecuted_draft_checkpoint_once(self) -> None:
        item_id = "KAMA-JOB"
        session_id = "27759029-d706-43ab-bebb-6a1ec85dc498"
        self.database.upsert_experiment(ExperimentSpec(
            id=item_id, strategy_name="KamaPullback",
        ))
        self.database.upsert_work_item(WorkItem(
            id=item_id, experiment_id=item_id, priority=1,
            state=WorkState.BLOCKED, attempts=4,
            blocker_code="retry_limit_reached",
            blocker_detail=(
                "jesse_start_recovery_failed after 5 attempts: session "
                f"{session_id} remains draft after prior start recovery"
            ),
        ))
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_sessions(
                       work_item_id,experiment_id,session_id,request_fingerprint,
                       state,error_text,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (item_id, item_id, session_id, "fingerprint",
                 "start_recovery_failed", "remained draft", "now", "now"),
            )
        evidence = {
            "id": session_id, "status": "draft", "metrics": None,
            "trades": [], "equity_curve": [], "execution_duration": None,
        }

        preview = recover_unexecuted_draft_checkpoint(
            self.database, item_id, session_id, evidence, apply=False,
        )
        applied = recover_unexecuted_draft_checkpoint(
            self.database, item_id, session_id, evidence, apply=True,
        )
        again = recover_unexecuted_draft_checkpoint(
            self.database, item_id, session_id, evidence, apply=True,
        )

        self.assertTrue(preview["eligible"])
        self.assertEqual(applied["transition"], "blocked->ready")
        self.assertEqual(again["transition"], "already_recovered")
        row = self.database.rows(
            "SELECT state,attempts FROM work_items WHERE id=?", (item_id,),
        )[0]
        self.assertEqual(row, {"state": "ready", "attempts": 0})
        self.assertEqual(self.database.rows(
            "SELECT COUNT(*) n FROM direct_execution_sessions WHERE work_item_id=?",
            (item_id,),
        )[0]["n"], 0)
        self.assertEqual(self.database.rows(
            "SELECT COUNT(*) n FROM events WHERE aggregate_id=? "
            "AND event_type='unexecuted_draft_checkpoint_archived'", (item_id,),
        )[0]["n"], 1)

    def test_zombie_recovery_dry_run_apply_and_repeat_are_evidence_safe(self) -> None:
        item_id = "ZOMBIE-JOB"
        session_id = "zombie-session"
        self.database.upsert_experiment(ExperimentSpec(
            id=item_id, strategy_name="FrozenStrategy",
        ))
        self.database.upsert_work_item(WorkItem(
            id=item_id, experiment_id=item_id, priority=1,
            state=WorkState.BLOCKED, attempts=4,
            blocker_code="retry_limit_reached",
            blocker_detail="jesse_execution_deferred after repeated polls",
        ))
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_sessions(
                       work_item_id,experiment_id,session_id,request_fingerprint,
                       state,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (item_id, item_id, session_id, "fingerprint", "running", "now", "now"),
            )
        session = {
            "id": session_id, "status": "running", "updated_at": 1_000,
            "execution_duration": None,
            "state": {"results": {
                "executing": False, "progressbar": {"current": 0},
                "metrics": {}, "trades": [], "charts": {"equity_curve": []},
                "exception": {"error": None, "traceback": None},
            }},
        }
        observations = {session_id: [session, dict(session)]}

        preview = recover_zombie_execution_sessions(
            self.database, observations, apply=False, grace_seconds=0,
        )
        self.assertEqual(len(preview["planned"]), 1)
        self.assertEqual(self.database.rows(
            "SELECT state,attempts FROM work_items WHERE id=?", (item_id,),
        )[0], {"state": "blocked", "attempts": 4})

        applied = recover_zombie_execution_sessions(
            self.database, observations, apply=True, grace_seconds=0,
        )
        again = recover_zombie_execution_sessions(
            self.database, observations, apply=True, grace_seconds=0,
        )
        self.assertEqual(applied["changed"], [item_id])
        self.assertEqual(again["already_recovered"], [item_id])
        self.assertEqual(again["changed"], [])
        self.assertEqual(self.database.rows(
            "SELECT state,attempts FROM work_items WHERE id=?", (item_id,),
        )[0], {"state": "ready", "attempts": 0})
        recovery = self.database.rows(
            "SELECT replacement_allowed,replacement_session_id "
            "FROM direct_execution_recoveries WHERE work_item_id=?", (item_id,),
        )[0]
        self.assertEqual(recovery["replacement_allowed"], 1)
        self.assertIsNone(recovery["replacement_session_id"])

    def test_zombie_recovery_never_invalidates_durable_terminal_evidence(self) -> None:
        item_id = "VALID-JOB"
        session_id = "valid-session"
        self.database.upsert_experiment(ExperimentSpec(
            id=item_id, strategy_name="ValidStrategy",
        ))
        self.database.upsert_work_item(WorkItem(
            id=item_id, experiment_id=item_id, priority=1,
            state=WorkState.BLOCKED, attempts=4,
        ))
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_sessions(
                       work_item_id,experiment_id,session_id,request_fingerprint,
                       state,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (item_id, item_id, session_id, "fingerprint", "running", "now", "now"),
            )
        self.database.add_run(RunResult(
            id="RUN-VALID", experiment_id=item_id, work_item_id=item_id,
            session_id=session_id, status=RunStatus.FINISHED,
            metrics={"net_profit_percentage": 1.0},
        ))
        session = {
            "id": session_id, "status": "running", "updated_at": 1_000,
            "state": {"results": {
                "executing": False, "progressbar": {"current": 0},
                "metrics": {}, "trades": [], "charts": {"equity_curve": []},
                "exception": {"error": None, "traceback": None},
            }},
        }
        result = recover_zombie_execution_sessions(
            self.database, {session_id: [session, session]},
            apply=True, grace_seconds=0,
        )
        self.assertEqual(result["changed"], [])
        self.assertEqual(result["rejected"][item_id], "durable_run_exists")

    def test_recovery_audit_classifies_dependencies_infrastructure_and_zombie(self) -> None:
        for item_id, code, dependencies in (
            ("DEP", "dependency_blocked", ("MISSING",)),
            ("INFRA", "direct_mcp_error", ()),
            ("ZOMBIE", "retry_limit_reached", ()),
        ):
            self.database.upsert_experiment(ExperimentSpec(
                id=item_id, strategy_name=item_id,
            ))
            self.database.upsert_work_item(WorkItem(
                id=item_id, experiment_id=item_id, priority=1,
                state=WorkState.BLOCKED, blocker_code=code,
                dependencies=dependencies,
            ))
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_sessions(
                       work_item_id,experiment_id,session_id,request_fingerprint,
                       state,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                ("ZOMBIE", "ZOMBIE", "zombie-session", "fp", "running", "now", "now"),
            )
        session = {
            "status": "running", "updated_at": 1_000,
            "state": {"results": {
                "executing": False, "progressbar": {"current": 0},
                "metrics": {}, "trades": [], "charts": {"equity_curve": []},
                "exception": {"error": None, "traceback": None},
            }},
        }
        result = classify_recovery_candidates(
            self.database, {"zombie-session": [session, session]},
        )
        self.assertEqual(
            [row["work_item_id"] for row in result["dependency_only_blockers"]],
            ["DEP"],
        )
        self.assertEqual(
            [row["work_item_id"] for row in result["infrastructure_transport_failures"]],
            ["INFRA"],
        )
        self.assertEqual(
            [row["work_item_id"] for row in result["stopped_or_nonstarted_without_evidence"]],
            ["ZOMBIE"],
        )

    def test_draft_recovery_rejects_any_execution_evidence(self) -> None:
        result = recover_unexecuted_draft_checkpoint(
            self.database, "JOB", "SESSION", {
                "id": "SESSION", "status": "draft", "metrics": None,
                "trades": [{"id": 1}], "equity_curve": [],
                "execution_duration": None,
            }, apply=False,
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "session_has_execution_evidence")

    def test_recover_only_verified_negative_margin_sizing_blocker(self) -> None:
        item_id = "DAILY-JOB"
        self.database.upsert_experiment(ExperimentSpec(
            id=item_id, strategy_name="DailyBalanceExpansionBreakout",
        ))
        self.database.upsert_work_item(WorkItem(
            id=item_id, experiment_id=item_id, priority=1,
            state=WorkState.BLOCKED, attempts=4,
            blocker_code="retry_limit_reached",
            blocker_detail=(
                "jesse_execution_stopped after 5 attempts: Cannot submit an order "
                "with a value of $-10 when your available margin is $-10."
            ),
        ))
        session_id = "97c37a3c-3446-4581-81eb-20ddf74e6768"
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_sessions(
                       work_item_id,experiment_id,session_id,request_fingerprint,
                       state,error_text,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (item_id, item_id, session_id, "old-fingerprint", "stopped",
                 "Cannot submit an order with a value of $-10 when your "
                 "available margin is $-10.", "now", "now"),
            )

        result = recover_verified_margin_sizing_blocker(
            self.database, item_id,
            strategy_name="DailyBalanceExpansionBreakout", apply=True,
        )
        again = recover_verified_margin_sizing_blocker(
            self.database, item_id,
            strategy_name="DailyBalanceExpansionBreakout", apply=True,
        )

        self.assertEqual(result["transition"], "blocked->ready")
        self.assertEqual(again["transition"], "already_recovered")
        self.assertEqual(self.database.rows(
            "SELECT state,attempts FROM work_items WHERE id=?", (item_id,),
        )[0], {"state": "ready", "attempts": 0})
        self.assertEqual(self.database.rows(
            "SELECT COUNT(*) n FROM direct_execution_sessions WHERE work_item_id=?",
            (item_id,),
        )[0]["n"], 0)
        events = self.database.rows(
            """SELECT payload_json FROM events WHERE aggregate_id=?
                 AND event_type='invalid_stopped_checkpoint_archived'""",
            (item_id,),
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]["payload_json"])["session_id"], session_id)

    def test_margin_recovery_preserves_stopped_checkpoint_with_valid_run(self) -> None:
        item_id = "DAILY-WITH-RUN"
        session_id = "valid-session"
        self.database.upsert_experiment(ExperimentSpec(
            id=item_id, strategy_name="DailyBalanceExpansionBreakout",
        ))
        self.database.upsert_work_item(WorkItem(
            id=item_id, experiment_id=item_id, priority=1,
            state=WorkState.BLOCKED, attempts=4,
            blocker_code="retry_limit_reached",
            blocker_detail=(
                "Cannot submit an order with a value of $-10 when your "
                "available margin is $-10."
            ),
        ))
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_sessions(
                       work_item_id,experiment_id,session_id,request_fingerprint,
                       state,error_text,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (item_id, item_id, session_id, "fingerprint", "stopped",
                 "negative margin", "now", "now"),
            )
        self.database.add_run(RunResult(
            id="valid-run", experiment_id=item_id, work_item_id=item_id,
            session_id=session_id, status=RunStatus.FINISHED,
            metrics={"net_profit": 1},
        ))

        result = recover_verified_margin_sizing_blocker(
            self.database, item_id,
            strategy_name="DailyBalanceExpansionBreakout", apply=True,
        )

        self.assertFalse(result["eligible"])
        self.assertEqual(result["reason"], "valid_run_exists")
        self.assertEqual(self.database.rows(
            "SELECT COUNT(*) n FROM direct_execution_sessions WHERE work_item_id=?",
            (item_id,),
        )[0]["n"], 1)


if __name__ == "__main__":
    unittest.main()
