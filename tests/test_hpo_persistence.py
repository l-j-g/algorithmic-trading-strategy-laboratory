from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    Evaluation,
    ExperimentSpec,
    ExperimentType,
    Verdict,
    WorkItem,
    WorkState,
)


class HpoPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = WorkflowDatabase(Path(self.temp.name) / "lab.sqlite3")
        self.database.initialize()
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-1",
            strategy_name="Trend",
            experiment_type=ExperimentType.BASELINE,
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-1",
            experiment_id="EXP-1",
            priority=1,
            state=WorkState.FINISHED,
        ))
        self.database.add_evaluation(Evaluation(
            experiment_id="EXP-1",
            verdict=Verdict.HPO_CANDIDATE,
            evaluator="test",
        ))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_candidate_filters_use_explicit_none_aware_predicates(self) -> None:
        by_id = self.database.hpo_studies({"id": "candidate:EXP-1"})
        self.assertEqual(len(by_id), 1)
        self.assertEqual(by_id[0]["study_id"], "candidate:EXP-1")

        by_name = self.database.hpo_studies({"study_name": "Trend candidate"})
        self.assertEqual([s["study_id"] for s in by_name], ["candidate:EXP-1"])

        null_target = self.database.hpo_studies({"hpo_work_item_id": None})
        self.assertEqual(
            [s["study_id"] for s in null_target], ["candidate:EXP-1"],
        )

        self.assertEqual(self.database.hpo_studies({"id": "candidate:OTHER"}), [])

    def test_candidate_schedule_completion_retry_and_terminal_disposition(self) -> None:
        candidates = self.database.hpo_studies(
            {"lifecycle_state": "hpo_candidate"},
        )
        self.assertEqual(candidates[0]["parent_experiment_id"], "EXP-1")

        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        repeated = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.assertEqual(study["id"], repeated["id"])
        self.assertEqual(study["lifecycle_state"], "hpo_scheduled")
        self.assertEqual(
            self.database.hpo_study_for_work_item(
                study["hpo_work_item_id"],
            )["study_id"],
            study["id"],
        )

        self.database.start_hpo_study(study["id"])
        job = self.database.complete_hpo_study(study["id"])
        claimed = self.database.claim_hpo_analysis(
            "analyzer", cohort_id="COHORT-1",
        )
        self.assertEqual(job["id"], claimed["id"])
        self.assertEqual(claimed["attempts"], 1)
        retry = self.database.retry_hpo_analysis(
            claimed["id"],
            error="temporary",
            retry_after="2000-01-01T00:00:00Z",
        )
        self.assertEqual(retry["state"], "waiting_retry")
        claimed = self.database.claim_hpo_analysis("analyzer")
        with self.assertRaises(ValueError):
            self.database.terminalize_hpo_analysis(
                claimed["id"],
                disposition="paper_trade_candidate",
                finding="Unsafe direct promotion.",
                next_action="Run validation.",
            )
        final = self.database.terminalize_hpo_analysis(
            claimed["id"],
            disposition="revise",
            finding="Needs wider validation.",
            next_action="Revise ranges.",
        )
        self.assertEqual(final["lifecycle_state"], "revise")
        self.assertEqual(final["disposition"], "revise")

    def test_abandoned_analysis_is_recoverable(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.complete_hpo_study(study["id"])
        job = self.database.claim_hpo_analysis("analyzer")
        self.database.abandon_hpo_analysis(job["id"], error="worker lost")

        recovered = self.database.recover_abandoned_hpo_analysis(
            "9999-12-31T00:00:00Z",
        )

        self.assertEqual([row["id"] for row in recovered], [job["id"]])
        self.assertEqual(
            self.database.current_analyzer_status()["state"], "pending",
        )

    def test_unroutable_running_study_returns_to_scheduled(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.start_hpo_study(study["id"])
        work_item_id = study["hpo_work_item_id"]
        self.database.promote_scheduled_runnable(1)

        changed = self.database.mark_unroutable_hpo_requirements_pending()

        self.assertEqual(changed, 1)
        refreshed = self.database.hpo_study_detail(study["id"])
        self.assertEqual(refreshed["lifecycle_state"], "hpo_scheduled")
        self.assertIsNone(refreshed["started_at"])
        work = self.database.rows(
            "SELECT state,blocker_code FROM work_items WHERE id=?",
            (work_item_id,),
        )[0]
        self.assertEqual(work["state"], "ready")
        self.assertEqual(work["blocker_code"], "requirements_pending")

    def test_empty_scheduled_hpo_is_parked_until_trials_are_imported(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.start_hpo_study(study["id"])

        job = self.database.complete_hpo_study(
            study["id"], require_trial_evidence=True,
        )

        self.assertEqual(job["state"], "waiting_retry")
        self.assertIn("hpo_trials_required", job["last_error"])
        self.assertIsNone(self.database.claim_hpo_analysis("analyzer"))
        work = self.database.rows(
            """SELECT state,blocker_code,blocker_detail,specification_json
               FROM work_items WHERE id=?""",
            (study["hpo_work_item_id"],),
        )[0]
        self.assertEqual(work["state"], "scheduled")
        self.assertEqual(work["blocker_code"], "hpo_trials_required")
        self.assertIn("Import completed optimizer trials", work["blocker_detail"])
        self.assertEqual(
            json.loads(work["specification_json"])["readiness"],
            {"missing": ["hpo_trials"], "status": "requirements_pending"},
        )
        event = self.database.rows(
            """SELECT payload_json FROM events
               WHERE aggregate_id=? AND event_type='hpo_trials_required'""",
            (study["id"],),
        )[0]
        self.assertIn("Import completed optimizer trials", event["payload_json"])

    def test_reconcile_finished_hpo_work_repairs_stuck_lifecycle(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.start_hpo_study(study["id"])
        self.database.transition_work_item(
            study["hpo_work_item_id"], WorkState.FINISHED,
            allowed_from=(WorkState.SCHEDULED,),
        )

        repaired = self.database.reconcile_finished_hpo_work()

        self.assertEqual(repaired, [study["id"]])
        detail = self.database.hpo_study_detail(study["id"])
        self.assertEqual(detail["lifecycle_state"], "hpo_analysis")
        self.assertEqual(detail["analysis_job"]["state"], "waiting_retry")
        self.assertIn("hpo_trials_required", detail["analysis_job"]["last_error"])

    def test_trialless_hpo_execution_can_be_requeued_after_provider_repair(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.start_hpo_study(study["id"])
        self.database.transition_work_item(
            study["hpo_work_item_id"], WorkState.FINISHED,
            allowed_from=(WorkState.SCHEDULED,),
        )
        self.database.reconcile_finished_hpo_work()

        result = self.database.requeue_hpo_execution(
            study["id"], reason="Agent provider repaired", updated_by="test",
        )

        self.assertEqual(result["lifecycle_state"], "hpo_scheduled")
        work = self.database.rows(
            "SELECT state,blocker_code,attempts FROM work_items WHERE id=?",
            (study["hpo_work_item_id"],),
        )[0]
        self.assertEqual(work, {
            "state": "ready", "blocker_code": None, "attempts": 0,
        })

    def test_hpo_execution_is_not_starved_by_lower_priority_backtests(self) -> None:
        self.database.upsert_work_item(WorkItem(
            id="JOB-2",
            experiment_id="EXP-1",
            priority=1,
            state=WorkState.READY,
        ))
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.start_hpo_study(study["id"])
        self.database.transition_work_item(
            study["hpo_work_item_id"], WorkState.FINISHED,
            allowed_from=(WorkState.SCHEDULED,),
        )
        self.database.reconcile_finished_hpo_work()
        self.database.requeue_hpo_execution(
            study["id"], reason="provider repaired", updated_by="test",
        )

        claimed = self.database.claim_batch("worker", 1)

        self.assertEqual([item["id"] for item in claimed], [study["hpo_work_item_id"]])

    def test_configured_routes_release_hpo_before_validation_jobs_exist(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.promote_scheduled_runnable(1)
        self.database.mark_unroutable_hpo_requirements_pending()

        result = self.database.configure_hpo_validation_routes(
            study["id"],
            {
                "oos": [{
                    "exchange": "Binance Perpetual Futures",
                    "symbol": "BTC-USDT", "timeframe": "1h",
                    "start_date": "2026-01-01", "finish_date": "2026-03-31",
                }],
                "rolling": [{
                    "exchange": "Binance Perpetual Futures",
                    "symbol": "BTC-USDT", "timeframe": "1h",
                    "start_date": "2025-01-01", "finish_date": "2026-03-31",
                }],
            },
        )

        self.assertEqual(result["hpo_routes"], 0)
        work = self.database.rows(
            "SELECT state,blocker_code,specification_json FROM work_items WHERE id=?",
            (study["hpo_work_item_id"],),
        )[0]
        self.assertEqual(work["state"], "ready")
        self.assertEqual(work["blocker_code"], "requirements_pending")
        self.assertEqual(
            json.loads(work["specification_json"])["readiness"]["status"],
            "requirements_pending",
        )
        experiment = self.database.rows(
            "SELECT specification_json FROM experiments WHERE id=?",
            (study["hpo_experiment_id"],),
        )[0]
        self.assertEqual(json.loads(experiment["specification_json"])["routes"], [])

        released = self.database.configure_hpo_validation_routes(
            study["id"],
            {"hpo": [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "BTC-USDT", "timeframe": "1h",
                "start_date": "2024-01-01", "finish_date": "2025-01-01",
            }]},
        )
        self.assertEqual(released["hpo_routes"], 1)
        work = self.database.rows(
            "SELECT state,blocker_code,specification_json FROM work_items WHERE id=?",
            (study["hpo_work_item_id"],),
        )[0]
        self.assertEqual(work["state"], "ready")
        self.assertIsNone(work["blocker_code"])
        self.assertEqual(
            json.loads(work["specification_json"])["readiness"]["status"],
            "ready",
        )
        experiment = self.database.rows(
            "SELECT specification_json FROM experiments WHERE id=?",
            (study["hpo_experiment_id"],),
        )[0]
        self.assertEqual(len(json.loads(experiment["specification_json"])["routes"]), 1)

    def test_route_configuration_rejects_training_validation_overlap(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        with self.assertRaisesRegex(ValueError, "overlaps hpo training"):
            self.database.configure_hpo_validation_routes(
                study["id"],
                {
                    "hpo": [{
                        "exchange": "Binance Perpetual Futures",
                        "symbol": "BTC-USDT", "timeframe": "1h",
                        "start_date": "2024-01-01", "finish_date": "2025-01-01",
                    }],
                    "oos": [{
                        "exchange": "Binance Perpetual Futures",
                        "symbol": "BTC-USDT", "timeframe": "1h",
                        "start_date": "2024-12-01", "finish_date": "2025-03-01",
                    }],
                },
            )

    def test_route_configuration_rejects_invalid_date_order(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        with self.assertRaisesRegex(ValueError, "start_date must precede"):
            self.database.configure_hpo_validation_routes(
                study["id"],
                {"hpo": [{
                    "exchange": "Binance Perpetual Futures",
                    "symbol": "BTC-USDT", "timeframe": "1h",
                    "start_date": "2025-01-01", "finish_date": "2024-01-01",
                }]},
            )

    def test_terminal_analysis_can_be_explicitly_requeued(self) -> None:
        study = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")
        self.database.complete_hpo_study(study["id"])
        job = self.database.claim_hpo_analysis("analyzer")
        terminal = self.database.retry_hpo_analysis(
            job["id"], error="provider quota",
            retry_after="2000-01-01T00:00:00Z", max_attempts=1,
        )

        reopened = self.database.requeue_terminal_hpo_analysis(
            terminal["id"], reason="fallback provider repaired",
            updated_by="test",
        )

        self.assertEqual(reopened["state"], "pending")
        self.assertEqual(reopened["attempts"], 0)
        self.assertIsNone(reopened["last_error"])
        self.assertEqual(
            self.database.hpo_studies(
                {"lifecycle_state": "hpo_analysis"},
            )[0]["study_id"],
            study["id"],
        )
        event = self.database.rows(
            """SELECT payload_json FROM events
               WHERE aggregate_id=? AND event_type='hpo_analysis_requeued'""",
            (job["id"],),
        )[0]
        self.assertIn("fallback provider repaired", event["payload_json"])

    def test_stage_timing_returns_normalized_shape(self) -> None:
        timing = self.database.record_work_item_stage(
            "JOB-1",
            "hpo_analysis",
            "2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:02Z",
            analyzer_attempt=2,
            cohort_id="COHORT-1",
            outcome="complete",
            detail={"trials": 50},
        )
        rows = self.database.recent_stage_timings()

        self.assertEqual(rows[0]["id"], timing["id"])
        self.assertEqual(rows[0]["attempt"], 2)
        self.assertEqual(rows[0]["duration_seconds"], 2.0)
        self.assertEqual(rows[0]["completed_at"], "2026-01-01T00:00:02Z")
        self.assertEqual(rows[0]["detail"], {"trials": 50})
        self.assertNotIn("detail_json", rows[0])
        self.assertNotIn("duration_ms", rows[0])


if __name__ == "__main__":
    unittest.main()
