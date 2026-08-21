from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ats_lab.dashboard import (
    DASHBOARD_JS, dashboard_counts, host_allows_mutations, host_header_allowed,
    make_handler, query_page, render_overview, render_page, serve,
    top_backtests,
)
from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    Evaluation,
    ExperimentType,
    ExperimentSpec,
    RunResult,
    RunStatus,
    RouteSpec,
    Verdict,
    WorkItem,
    WorkState,
)


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = WorkflowDatabase(Path(self.temporary.name) / "lab.sqlite3")
        self.database.initialize()
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="SafeStrategy", experiment_type=ExperimentType.BASELINE,
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=2, state=WorkState.BLOCKED,
            blocker_code="missing_data", blocker_detail="Need <script>alert(1)</script>",
        ))
        self.database.add_run(RunResult(
            id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="SESSION-1", status=RunStatus.FINISHED,
            dashboard_url="http://127.0.0.1/session",
            route=RouteSpec(
                exchange="Binance Perpetual Futures", symbol="BTC-USDT",
                timeframe="1h", start_date="2024-01-01",
                finish_date="2024-12-31",
            ),
            metrics={
                "sharpe": 1.25, "total_trades": 42,
                "net_profit_percentage": 12.5,
                "max_drawdown_percentage": 8.0,
                "evidence_split": "holdout",
                "raw_secret": "diagnostic-only",
            },
            finished_at="2026-07-30T01:00:00Z",
        ))
        self.database.add_evaluation(Evaluation(
            experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
            summary="Promising", metrics_summary="legacy summary",
            next_step="Run OOS", evaluator="test",
        ))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_counts_separate_queue_running_blockers_and_candidates(self) -> None:
        self.assertEqual(dashboard_counts(self.database), {
            "queue": 1, "running": 0, "blocked": 1, "candidates": 1,
            "retry": 0, "hpo_active": 0, "analyzer": "idle",
            "awaiting_evaluation": 0,
            "remaining_chains": 0,
        })

    def test_search_filter_and_sort_are_whitelisted(self) -> None:
        rows, clean = query_page(self.database, "queue", {
            "q": "Safe", "state": "blocked", "sort": "priority; DROP TABLE runs",
        })
        self.assertEqual([row["id"] for row in rows], ["JOB-1"])
        self.assertEqual(clean["sort"], "priority")
        self.assertEqual(self.database.rows("SELECT COUNT(*) AS count FROM runs")[0]["count"], 1)

    def test_candidate_and_run_history_expose_only_normalized_evidence(self) -> None:
        candidates, _ = query_page(self.database, "candidates", {"verdict": "hpo_candidate"})
        runs, _ = query_page(self.database, "runs", {"status": "finished"})
        self.assertEqual(candidates[0]["run_id"], "RUN-1")
        self.assertEqual(candidates[0]["finding"], "Promising")
        self.assertEqual(candidates[0]["next_action"], "Run OOS")
        self.assertEqual(runs[0]["sharpe_ratio"], 1.25)
        self.assertEqual(runs[0]["trade_count"], 42)
        self.assertNotIn("metrics_json", runs[0])
        self.assertNotIn("raw_secret", runs[0])

    def test_render_escapes_database_content_and_adds_safe_dashboard_link(self) -> None:
        page = render_page(self.database, "queue", {})
        run_page = render_page(self.database, "runs", {})
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn('data-work-item-action="retry"', page)
        self.assertIn('data-work-item-action="rectify"', page)
        self.assertIn("<summary>standardized</summary>", run_page)
        self.assertNotIn("diagnostic-only", run_page)
        self.assertIn("—", run_page)

    def test_queue_renders_blocked_controls_and_waiting_retry_status(self) -> None:
        self.database.upsert_work_item(WorkItem(
            id="JOB-RETRY", experiment_id="EXP-1", priority=3,
            state=WorkState.WAITING_RETRY, attempts=1,
            retry_after="2099-01-01T00:00:00Z",
            blocker_code="temporary_failure", blocker_detail="Try later",
        ))

        page = render_page(self.database, "queue", {})

        blocked_start = page.index("<tr><td>JOB-1</td>")
        blocked_end = page.index("</tr>", blocked_start)
        blocked_cell = page[blocked_start:blocked_end]
        self.assertIn('data-work-item-action="retry"', blocked_cell)
        self.assertIn('data-work-item-action="rectify"', blocked_cell)

        retry_start = page.index("<tr><td>JOB-RETRY</td>")
        retry_end = page.index("</tr>", retry_start)
        retry_cell = page[retry_start:retry_end]
        self.assertIn("retry scheduled", retry_cell)
        self.assertNotIn('data-work-item-action="retry"', retry_cell)
        self.assertNotIn('data-work-item-action="rectify"', retry_cell)

    def test_empty_filtered_result_renders_cleanly(self) -> None:
        page = render_page(self.database, "queue", {"q": "does-not-exist"})
        self.assertIn("No matching records.", page)

    def test_metric_normalization_and_top_backtest_ranking(self) -> None:
        rows = top_backtests(self.database, "sharpe", 20, 20)
        self.assertEqual(rows[0]["run_id"], "RUN-1")
        self.assertEqual(rows[0]["score"], 1.25)
        self.assertEqual(rows[0]["evidence_split"], "holdout")
        self.assertEqual(top_backtests(self.database, "sharpe", 20, 100), [])

    def test_unsplit_route_complete_baseline_is_rankable(self) -> None:
        self.database.add_run(RunResult(
            id="RUN-BASELINE", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="SESSION-BASELINE", status=RunStatus.FINISHED,
            route=RouteSpec(
                exchange="Binance Perpetual Futures", symbol="BTC-USDT",
                timeframe="1h", start_date="2025-01-01",
                finish_date="2026-01-01",
            ),
            metrics={"sharpe": 2.0, "total_trades": 42},
            finished_at="2026-08-01T01:00:00Z",
        ))

        rows = top_backtests(self.database, "sharpe", 20, 20)

        self.assertEqual([row["run_id"] for row in rows], ["RUN-BASELINE"])

    def test_partial_comparison_filters_keep_unselected_dimensions(self) -> None:
        self.database.add_run(RunResult(
            id="RUN-OTHER-PAIR", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="SESSION-OTHER-PAIR", status=RunStatus.FINISHED,
            route=RouteSpec(
                exchange="Binance Perpetual Futures", symbol="ETH-USDT",
                timeframe="1h", start_date="2024-01-01",
                finish_date="2025-01-01",
            ),
            metrics={"sharpe": 1.5, "total_trades": 42},
            finished_at="2026-08-01T01:00:00Z",
        ))
        self.database.add_run(RunResult(
            id="RUN-OTHER-PERIOD", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="SESSION-OTHER-PERIOD", status=RunStatus.FINISHED,
            route=RouteSpec(
                exchange="Binance Perpetual Futures", symbol="BTC-USDT",
                timeframe="1h", start_date="2025-01-01",
                finish_date="2026-01-01",
            ),
            metrics={"sharpe": 1.25, "total_trades": 42},
            finished_at="2026-08-01T02:00:00Z",
        ))

        period_rows = top_backtests(
            self.database, "sharpe", 20, 20, period="2024-01-01 to 2025-01-01",
        )
        symbol_rows = top_backtests(
            self.database, "sharpe", 20, 20, symbol="BTC-USDT",
        )

        self.assertEqual([row["run_id"] for row in period_rows], ["RUN-OTHER-PAIR"])
        self.assertEqual(
            {row["run_id"] for row in symbol_rows},
            {"RUN-1", "RUN-OTHER-PERIOD"},
        )

    def test_default_comparison_uses_one_complete_compatibility_tuple(self) -> None:
        self.database.add_run(RunResult(
            id="RUN-2", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="SESSION-2", status=RunStatus.FINISHED,
            route=RouteSpec(
                exchange="Binance Perpetual Futures", symbol="ETH-USDT",
                timeframe="4h", start_date="2025-01-01",
                finish_date="2025-12-31",
            ),
            metrics={
                "sharpe_ratio": 9.0, "trade_count": 100,
                "evidence_split": "oos",
            },
            finished_at="2026-07-30T02:00:00Z",
        ))

        rows = top_backtests(self.database)
        candidates, _ = query_page(self.database, "candidates", {})
        evidence_rows, _ = query_page(self.database, "runs", {})

        self.assertEqual([row["run_id"] for row in rows], ["RUN-2"])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(len(evidence_rows), 2)
        self.assertEqual(
            {
                (
                    row["symbol"], row["timeframe"], row["start_date"],
                    row["finish_date"], row["evidence_split"],
                )
                for row in rows
            },
            {("ETH-USDT", "4h", "2025-01-01", "2025-12-31", "oos")},
        )

    def test_overview_contains_chart_and_live_controls(self) -> None:
        page = render_overview(self.database, {})
        self.assertIn("Top 20 comparable results", page)
        self.assertIn('id=top-chart', page)
        self.assertIn('/assets/dashboard.js', page)
        self.assertIn("SafeStrategy", page)
        self.assertIn("pair", page)
        self.assertIn("split", page)
        self.assertIn("rank: sharpe", page)
        self.assertIn("net profit", page)
        self.assertIn("standardized", page)
        self.assertIn("refresh(); schedule();", DASHBOARD_JS)

    def test_hpo_lifecycle_page_uses_shared_states_and_progress(self) -> None:
        study = {
            "study_id": "HPO-1", "name": "study", "strategy": "SafeStrategy",
            "parent_experiment_id": "EXP-1", "parent_work_item_id": "JOB-1",
            "hpo_experiment_id": "EXP-HPO", "hpo_work_item_id": "JOB-HPO",
            "lifecycle_state": "hpo_analysis", "objective_name": "sharpe_ratio",
            "direction": "maximize", "trial_count": 100,
            "completed_trial_count": 80, "selected_trial_count": 3,
            "validation_count": 1, "disposition": None,
            "finding": "analyzing", "next_action": "select trials",
            "started_at": "2026-07-30T00:00:00Z", "completed_at": None,
            "updated_at": "2026-07-30T01:00:00Z",
        }
        with patch.object(
            WorkflowDatabase, "hpo_studies", return_value=[study],
        ):
            page = render_page(self.database, "hpo", {})

        self.assertIn("hpo_analysis", page)
        self.assertIn("80/100", page)
        self.assertIn("select trials", page)
        self.assertIn("/hpo/HPO-1", page)
        self.assertNotIn("params_json", page)

    def test_hpo_page_consumes_persisted_public_contract(self) -> None:
        scheduled = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")

        page = render_page(self.database, "hpo", {})

        self.assertIn(scheduled["id"], page)
        self.assertIn("hpo_scheduled", page)
        self.assertIn("SafeStrategy", page)

    def test_unscheduled_hpo_candidate_appears_in_same_lifecycle(self) -> None:
        page = render_page(self.database, "hpo", {})

        self.assertIn("candidate:EXP-1", page)
        self.assertIn("hpo_candidate", page)

    def test_unscheduled_candidate_has_normal_detail_page(self) -> None:
        from ats_lab.dashboard import render_hpo_detail_page

        page = render_hpo_detail_page(self.database, "candidate:EXP-1")

        self.assertIsNotNone(page)
        self.assertIn("hpo_candidate", page)
        self.assertNotIn("params_json", page)

    def test_hpo_detail_links_evidence_and_shows_timing_and_analyzer(self) -> None:
        detail = {
            "study": {
                "study_id": "HPO-1", "strategy": "SafeStrategy",
                "lifecycle_state": "validation", "trial_count": 100,
                "completed_trial_count": 100, "selected_trial_count": 1,
                "validation_count": 1, "finding": "validate OOS",
                "next_action": "complete validation",
            },
            "selected_trials": [{
                "rank": 1, "trial_number": 4, "objective_value": 1.5,
                "classification": "selected", "evidence_key": "KEY-1",
                "run_id": "RUN-1", "session_id": "SESSION-1",
                "selection_reason": "best holdout",
            }],
            "validations": [{
                "status": "scheduled",
                "readiness_status": "requirements_pending",
                "blocker_detail": "validation routes required",
                "experiment_id": "VAL-1",
                "run_id": "RUN-VAL", "session_id": "SESSION-VAL",
                "finding": None,
            }],
            "timings": [{
                "stage": "hpo_analysis", "attempt": 1, "state": "completed",
                "duration_seconds": 90, "outcome": "selected",
                "started_at": "2026-07-30T00:00:00Z",
                "completed_at": "2026-07-30T00:01:30Z",
            }],
            "analysis_job": {
                "job_id": "ANALYZE-1", "state": "completed", "attempts": 1,
                "retry_after": None, "last_error": None,
            },
        }
        with patch.object(
            WorkflowDatabase, "hpo_study_detail", return_value=detail,
        ):
            from ats_lab.dashboard import render_hpo_detail_page
            page = render_hpo_detail_page(self.database, "HPO-1")

        self.assertIn("validation", page)
        self.assertIn("/runs?q=RUN-1", page)
        self.assertIn("/runs?q=SESSION-1", page)
        self.assertIn("1m 30s", page)
        self.assertIn("ANALYZE-1", page)
        self.assertIn("requirements_pending", page)
        self.assertIn("validation routes required", page)
        self.assertNotIn("params_json", page)

    def test_json_api_and_security_headers(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.database))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/top-backtests") as response:
                body = json.load(response)
                self.assertEqual(body[0]["run_id"], "RUN-1")
                self.assertNotIn("metrics_json", body[0])
                self.assertEqual(response.headers.get_content_type(), "application/json")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/runs") as response:
                body = json.load(response)
                self.assertNotIn("metrics_json", body["rows"][0])
                self.assertNotIn("raw_secret", json.dumps(body))
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/diagnostics/runs/RUN-1"
            ) as response:
                body = json.load(response)
                self.assertEqual(body["metrics"]["raw_secret"], "diagnostic-only")
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/synthesis-status") as response:
                body = json.load(response)
                self.assertIn("remaining_chains", body)
                self.assertEqual(response.headers.get_content_type(), "application/json")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/hpo-studies"
            ) as response:
                body = json.load(response)
                self.assertEqual(body[0]["lifecycle_state"], "hpo_candidate")
                self.assertNotIn("params_json", json.dumps(body))
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/hpo-studies/candidate%3AEXP-1"
            ) as response:
                body = json.load(response)
                self.assertEqual(body["study_id"], "candidate:EXP-1")
                self.assertEqual(body["selected_trials"], [])
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/analyzer-status"
            ) as response:
                self.assertIsNone(json.load(response))
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/lifecycle-timings"
            ) as response:
                self.assertEqual(json.load(response), [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_live_refresh_contract_returns_fragments_and_json(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.database))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for page in ("queue", "candidates", "runs", "hpo"):
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{server.server_port}/{page}?fragment=1"
                ) as response:
                    body = response.read().decode()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers.get_content_type(), "text/html")
                    self.assertTrue(body.startswith("<section class=cards>"))
                    self.assertNotIn("<!doctype", body.lower())

            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/summary"
            ) as response:
                summary = json.load(response)
                self.assertEqual(response.status, 200)
                self.assertEqual(summary["queue"], 1)

            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/top-backtests?limit=20"
            ) as response:
                rows = json.load(response)
                self.assertEqual(response.status, 200)
                self.assertIsInstance(rows, list)
                self.assertEqual(rows[0]["run_id"], "RUN-1")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_queue_actions_require_confirmation_and_write_audit_events(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.database))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(
                f"{base}/api/work-items/JOB-1/retry",
                data=b"{}", method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 428)
            self.assertEqual(
                self.database.rows("SELECT state FROM work_items WHERE id='JOB-1'")[0]["state"],
                "blocked",
            )

            request = urllib.request.Request(
                f"{base}/api/work-items/JOB-1/retry",
                data=b'{"reason":"operator test retry"}',
                headers={"X-ATS-Lab-Confirm": "retry"}, method="POST",
            )
            with urllib.request.urlopen(request) as response:
                payload = json.load(response)
            self.assertEqual(payload["action"], "retry")
            self.assertEqual(payload["state"], "ready")
            self.assertEqual(
                self.database.rows(
                    "SELECT attempts,blocker_code FROM work_items WHERE id='JOB-1'"
                )[0],
                {"attempts": 0, "blocker_code": None},
            )
            self.assertEqual(
                self.database.rows(
                    "SELECT COUNT(*) AS count FROM events WHERE aggregate_id='JOB-1' AND event_type='blocker_retry_requested'"
                )[0]["count"],
                1,
            )

            self.database.transition_work_item(
                "JOB-1", WorkState.BLOCKED, allowed_from=(WorkState.READY,),
                blocker_code="analyzer_retry_exhausted",
                blocker_detail="old analyzer failure",
            )
            request = urllib.request.Request(
                f"{base}/api/work-items/JOB-1/rectify",
                data=b'{"reason":"operator test rectify"}',
                headers={"X-ATS-Lab-Confirm": "rectify"}, method="POST",
            )
            with urllib.request.urlopen(request) as response:
                payload = json.load(response)
            self.assertEqual(payload["action"], "rectify")
            self.assertEqual(payload["state"], "archived")
            self.assertEqual(
                self.database.rows(
                    "SELECT state,blocker_code FROM work_items WHERE id='JOB-1'"
                )[0],
                {"state": "archived", "blocker_code": "legacy_history"},
            )
            self.assertEqual(
                self.database.rows(
                    "SELECT COUNT(*) AS count FROM events WHERE aggregate_id='JOB-1' AND event_type='blocker_rectified'"
                )[0]["count"],
                1,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_mutations_are_refused_when_handler_disallows_them(self) -> None:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.database, allow_mutations=False),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(
                f"{base}/api/work-items/JOB-1/retry",
                data=b'{"reason":"should be refused"}',
                headers={"X-ATS-Lab-Confirm": "retry"}, method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            body = json.load(error.exception)
            self.assertEqual(body["error"]["code"], "mutations_disabled")

            request = urllib.request.Request(
                f"{base}/api/work-items/JOB-1/rectify",
                data=b"{}", headers={"X-ATS-Lab-Confirm": "rectify"},
                method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)

            with urllib.request.urlopen(f"{base}/api/summary") as response:
                self.assertEqual(response.status, 200)

            self.assertEqual(
                self.database.rows(
                    "SELECT state FROM work_items WHERE id='JOB-1'"
                )[0]["state"],
                "blocked",
            )
            self.assertEqual(
                self.database.rows(
                    "SELECT COUNT(*) AS count FROM events "
                    "WHERE event_type IN "
                    "('blocker_retry_requested','blocker_rectified')"
                )[0]["count"],
                0,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_only_loopback_binds_enable_mutations(self) -> None:
        self.assertTrue(host_allows_mutations("127.0.0.1"))
        self.assertTrue(host_allows_mutations("localhost"))
        self.assertTrue(host_allows_mutations("::1"))
        self.assertFalse(host_allows_mutations("0.0.0.0"))
        self.assertFalse(host_allows_mutations("192.168.1.10"))

    def test_serve_wires_mutation_gate_to_bind_host(self) -> None:
        for host, expected_code in (("127.0.0.1", 428), ("0.0.0.0", 403)):
            recording: dict[str, object] = {}

            class FakeServer:
                server_port = 8765

                def __init__(self, address, handler) -> None:
                    recording["address"] = address
                    recording["handler"] = handler

                def serve_forever(self) -> None:
                    raise KeyboardInterrupt

                def server_close(self) -> None:
                    return

            with (
                patch("ats_lab.dashboard.ThreadingHTTPServer", FakeServer),
                redirect_stdout(io.StringIO()),
            ):
                serve(self.database, host, 8765)

            self.assertEqual(recording["address"], (host, 8765))
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), recording["handler"],
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_port}"
                    "/api/work-items/JOB-1/retry",
                    data=b'{"reason":"gate probe"}', method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, expected_code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            self.assertEqual(
                self.database.rows(
                    "SELECT state FROM work_items WHERE id='JOB-1'"
                )[0]["state"],
                "blocked",
            )


class HostHeaderGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = WorkflowDatabase(Path(self.temporary.name) / "lab.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _serve(self, **kwargs: object) -> str:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.database, **kwargs),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def _get(self, base: str, host: str | None = None):
        headers = {"Host": host} if host else {}
        request = urllib.request.Request(f"{base}/api/summary", headers=headers)
        return urllib.request.urlopen(request)

    def test_loopback_host_headers_with_ports_are_accepted(self) -> None:
        base = self._serve()
        port = base.rsplit(":", 1)[-1]

        with urllib.request.urlopen(f"{base}/api/summary") as response:
            self.assertEqual(response.status, 200)
        with self._get(base, f"localhost:{port}") as response:
            self.assertEqual(response.status, 200)
        with self._get(base, f"[::1]:{port}") as response:
            self.assertEqual(response.status, 200)

    def test_foreign_host_header_is_rejected_with_403(self) -> None:
        base = self._serve()

        with self.assertRaises(urllib.error.HTTPError) as error:
            self._get(base, "attacker.example")
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(
            json.load(error.exception)["error"]["code"], "forbidden_host",
        )

    def test_post_rejects_foreign_host_before_confirmation_gate(self) -> None:
        base = self._serve()

        request = urllib.request.Request(
            f"{base}/api/work-items/JOB-1/retry",
            data=b"{}", method="POST", headers={"Host": "attacker.example"},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_explicit_non_loopback_bind_accepts_literal_host_only(self) -> None:
        base = self._serve(bound_host="192.168.10.10")

        with self._get(base, "192.168.10.10") as response:
            self.assertEqual(response.status, 200)
        with self._get(base, "192.168.10.10:8765") as response:
            self.assertEqual(response.status, 200)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self._get(base, "other.example:9999")
        self.assertEqual(error.exception.code, 403)

    def test_host_header_parsing_variants(self) -> None:
        self.assertTrue(host_header_allowed("localhost:8123", "127.0.0.1"))
        self.assertTrue(host_header_allowed("[::1]:8123", "127.0.0.1"))
        self.assertTrue(host_header_allowed("::1", "127.0.0.1"))
        self.assertTrue(host_header_allowed(" 127.0.0.1 ", "127.0.0.1"))
        self.assertFalse(host_header_allowed("", "127.0.0.1"))
        self.assertFalse(host_header_allowed("attacker.example", "127.0.0.1"))
        self.assertFalse(host_header_allowed("127.0.0.1.evil.example", "127.0.0.1"))
        self.assertTrue(host_header_allowed("localhost:8123", "192.168.10.10"))
        self.assertFalse(host_header_allowed("attacker.example", "192.168.10.10"))


if __name__ == "__main__":
    unittest.main()
