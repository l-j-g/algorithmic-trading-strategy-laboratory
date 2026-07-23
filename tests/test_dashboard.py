from __future__ import annotations

import tempfile
import threading
import unittest
import urllib.request
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

from ats_lab.dashboard import (
    dashboard_counts, make_handler, normalize_metrics, query_page, render_overview,
    render_page, top_backtests,
)
from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    Evaluation,
    ExperimentType,
    ExperimentSpec,
    RunResult,
    RunStatus,
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
            metrics={"sharpe": 1.25, "total_trades": 42, "net_profit_percentage": 12.5},
        ))
        self.database.add_evaluation(Evaluation(
            experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
            summary="Promising", metrics_summary="sharpe=1.25", evaluator="test",
        ))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_counts_separate_queue_running_blockers_and_candidates(self) -> None:
        self.assertEqual(dashboard_counts(self.database), {
            "queue": 1, "running": 0, "blocked": 1, "candidates": 1,
            "remaining_chains": 0,
        })

    def test_search_filter_and_sort_are_whitelisted(self) -> None:
        rows, clean = query_page(self.database, "queue", {
            "q": "Safe", "state": "blocked", "sort": "priority; DROP TABLE runs",
        })
        self.assertEqual([row["id"] for row in rows], ["JOB-1"])
        self.assertEqual(clean["sort"], "priority")
        self.assertEqual(self.database.rows("SELECT COUNT(*) AS count FROM runs")[0]["count"], 1)

    def test_candidate_and_run_history_expose_metrics(self) -> None:
        candidates, _ = query_page(self.database, "candidates", {"verdict": "hpo_candidate"})
        runs, _ = query_page(self.database, "runs", {"status": "finished"})
        self.assertEqual(candidates[0]["run_count"], 1)
        self.assertEqual(candidates[0]["metrics_summary"], "sharpe=1.25")
        self.assertIn('"sharpe": 1.25', runs[0]["metrics_json"])
        self.assertEqual(runs[0]["sharpe_ratio"], 1.25)
        self.assertEqual(runs[0]["total_trades"], 42.0)

    def test_render_escapes_database_content_and_adds_safe_dashboard_link(self) -> None:
        page = render_page(self.database, "queue", {})
        run_page = render_page(self.database, "runs", {})
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn('rel="noopener noreferrer"', run_page)

    def test_empty_filtered_result_renders_cleanly(self) -> None:
        page = render_page(self.database, "queue", {"q": "does-not-exist"})
        self.assertIn("No matching records.", page)

    def test_metric_normalization_and_top_backtest_ranking(self) -> None:
        self.assertEqual(normalize_metrics('{"sharpe_ratio":2,"trade_count":30}')["total_trades"], 30.0)
        self.assertIsNone(normalize_metrics("not-json")["sharpe_ratio"])
        rows = top_backtests(self.database, "sharpe", 20, 20)
        self.assertEqual(rows[0]["id"], "RUN-1")
        self.assertEqual(rows[0]["score"], 1.25)
        self.assertEqual(top_backtests(self.database, "sharpe", 20, 100), [])

    def test_overview_contains_chart_and_live_controls(self) -> None:
        page = render_overview(self.database, {})
        self.assertIn("Top 20 comparable results", page)
        self.assertIn('id=top-chart', page)
        self.assertIn('/assets/dashboard.js', page)
        self.assertIn("SafeStrategy", page)
        self.assertIn("pair", page)
        self.assertIn("rank: sharpe", page)
        self.assertIn("net profit", page)

    def test_json_api_and_security_headers(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.database))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/top-backtests") as response:
                body = json.load(response)
                self.assertEqual(body[0]["id"], "RUN-1")
                self.assertEqual(response.headers.get_content_type(), "application/json")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/synthesis-status") as response:
                body = json.load(response)
                self.assertIn("remaining_chains", body)
                self.assertEqual(response.headers.get_content_type(), "application/json")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
