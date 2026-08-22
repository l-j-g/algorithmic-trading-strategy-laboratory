from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

from ats_lab.database import WorkflowDatabase
from ats_lab.models import (
    Evaluation, ExperimentSpec, ExperimentType, RouteSpec, RunResult,
    RunStatus, Verdict, WorkItem, WorkState,
)
from ats_lab.local_commands import LocalCommandError
from ats_lab.web_api import ControlService, ReadOnlyApi, make_handler


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = WorkflowDatabase(Path(self.temporary.name) / "lab.sqlite3")
        self.database.initialize()
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="SafeStrategy",
            experiment_type=ExperimentType.BASELINE,
            hypothesis="Pullbacks continue after a controlled trend reclaim.",
            target_regime="Directional trend",
            failure_regime="Range-bound chop",
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1,
            state=WorkState.READY,
        ))
        self.database.add_run(RunResult(
            id="RUN-1", experiment_id="EXP-1", work_item_id="JOB-1",
            session_id="SESSION-1", status=RunStatus.FINISHED,
            dashboard_url="http://127.0.0.1/session",
            route=RouteSpec(
                exchange="Binance Perpetual Futures", symbol="BTC-USDT",
                timeframe="1h", start_date="2026-01-01", finish_date="2026-01-31",
            ),
            metrics={"trade_count": 12, "net_profit_percentage": 3.5},
            finished_at="2026-08-11T00:00:00Z",
        ))
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO events(
                       aggregate_type,aggregate_id,event_type,payload_json,occurred_at
                   ) VALUES (?,?,?,?,?)""",
                (
                    "work_item", "JOB-1", "state_changed",
                    json.dumps({"secret": "must-not-leak"}),
                    "2026-08-11T00:00:00Z",
                ),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_snapshots_are_typed_read_only_and_hide_event_payload(self) -> None:
        before = self.database.rows(
            "SELECT COUNT(*) AS count FROM events",
        )[0]["count"]
        api = ReadOnlyApi(self.database)

        health = api.health_snapshot()
        summary = api.summary_snapshot()
        events = api.event_snapshots()

        after = self.database.rows(
            "SELECT COUNT(*) AS count FROM events",
        )[0]["count"]
        self.assertTrue(health.read_only)
        self.assertTrue(health.healthy)
        self.assertEqual(summary.work_states["ready"], 1)
        self.assertEqual(summary.active, 1)
        self.assertEqual(before, after)
        self.assertEqual(events[0].event_type, "state_changed")
        self.assertNotIn("payload_json", events[0].to_dict())
        self.assertNotIn("must-not-leak", json.dumps(events[0].to_dict()))

    def test_event_limit_is_bounded(self) -> None:
        api = ReadOnlyApi(self.database)
        self.assertEqual(len(api.event_snapshots(1)), 1)
        with self.assertRaises(ValueError):
            api.event_snapshots(0)
        with self.assertRaises(ValueError):
            api.event_snapshots(101)

    def test_summary_and_queue_distinguish_stale_claim_from_active_running(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE work_items SET state='running',claimed_by='dead-worker',
                   claimed_at='2026-01-01T00:00:00Z' WHERE id='JOB-1'"""
            )

        api = ReadOnlyApi(self.database)
        summary = api.summary_snapshot().to_dict()
        queue = api.page_snapshot("queue")["rows"]

        self.assertEqual(summary["active_running_claims"], 0)
        self.assertEqual(summary["stale_execution_claims"], 1)
        self.assertEqual(queue[0]["state"], "stale_claim")
        self.assertEqual(queue[0]["canonical_state"], "running")

        detail = api.work_item_detail("JOB-1")
        self.assertEqual(detail["work_item"]["state"], "stale_claim")
        self.assertNotIn("payload_json", json.dumps(detail))

    def test_backtest_and_experiment_details_expose_lineage_without_raw_payload(self) -> None:
        api = ReadOnlyApi(self.database)

        snapshot = api.backtest_snapshot()
        self.assertEqual(snapshot["rows"][0]["hypothesis"], "Pullbacks continue after a controlled trend reclaim.")
        self.assertEqual(snapshot["rows"][0]["experiment_type"], "baseline")
        self.assertEqual(snapshot["rows"][0]["test_type"], "backtest")
        self.assertEqual(snapshot["rows"][0]["dashboard_url"], "http://127.0.0.1/session")
        self.assertNotIn("metrics_json", snapshot["rows"][0])

        detail = api.experiment_detail("EXP-1")
        self.assertEqual(detail["experiment"]["hypothesis"], "Pullbacks continue after a controlled trend reclaim.")
        self.assertEqual(detail["evidence"][0]["target_regime"], "Directional trend")
        self.assertNotIn("source_path", detail["experiment"])

        evidence = api.evidence_detail("RUN-1")
        self.assertEqual(evidence["experiment"]["id"], "EXP-1")
        self.assertEqual(evidence["evidence"][0]["hypothesis"], "Pullbacks continue after a controlled trend reclaim.")

    def test_backtest_period_filters_bound_route_dates_inclusively(self) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO normalized_evidence(
                       evidence_key,schema_version,experiment_id,strategy,
                       lifecycle_stage,verdict,start_date,finish_date,
                       trade_count,net_profit_percentage,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "undated", 2, "EXP-1", "SafeStrategy", "baseline", "pass",
                    None, None, 5, 1.0, "2026-08-11T00:00:00Z",
                ),
            )
        api = ReadOnlyApi(self.database)

        included = api.backtest_snapshot({"started_on_or_after": "2026-01-01"})
        self.assertEqual([row["run_id"] for row in included["rows"]], ["RUN-1"])
        self.assertEqual(included["filters"]["started_on_or_after"], "2026-01-01")

        self.assertEqual(
            [row["run_id"] for row in api.backtest_snapshot({"started_on_or_after": "2026-01-02"})["rows"]],
            [],
        )

        included = api.backtest_snapshot({"finished_on_or_before": "2026-01-31"})
        self.assertEqual([row["run_id"] for row in included["rows"]], ["RUN-1"])
        self.assertEqual(included["filters"]["finished_on_or_before"], "2026-01-31")

        self.assertEqual(
            [row["run_id"] for row in api.backtest_snapshot({"finished_on_or_before": "2026-01-30"})["rows"]],
            [],
        )

        window = api.backtest_snapshot({
            "started_on_or_after": "2025-12-01",
            "finished_on_or_before": "2026-02-28",
        })
        self.assertEqual([row["run_id"] for row in window["rows"]], ["RUN-1"])

        undated = api.backtest_snapshot({"started_on_or_after": "2020-01-01"})
        self.assertNotIn("undated", json.dumps(undated))

        with self.assertRaises(ValueError):
            api.backtest_snapshot({"started_on_or_after": "not-a-date"})
        with self.assertRaises(ValueError):
            api.backtest_snapshot({"finished_on_or_before": "2026-13-40"})


    def test_http_surface_is_get_only_and_returns_json_snapshots(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.database))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(f"{base}/api/health") as response:
                health = json.load(response)
                self.assertTrue(health["healthy"])
                self.assertTrue(health["read_only"])
                self.assertEqual(response.headers["Cache-Control"], "no-store")

            with urllib.request.urlopen(f"{base}/api/summary") as response:
                summary = json.load(response)
                self.assertEqual(summary["work_states"]["ready"], 1)
                self.assertEqual(response.headers.get_content_type(), "application/json")

            with urllib.request.urlopen(f"{base}/api/v1/queue?state=ready") as response:
                queue = json.load(response)
                self.assertEqual(queue["page"], "queue")
                self.assertEqual(len(queue["rows"]), 1)

            with urllib.request.urlopen(f"{base}/api/v1/hpo/studies") as response:
                hpo = json.load(response)
                self.assertEqual(hpo["page"], "hpo")
                self.assertIsInstance(hpo["rows"], list)

            with urllib.request.urlopen(f"{base}/api/v1/control") as response:
                control = json.load(response)
                self.assertEqual(control["control"]["desired_state"], "running")

            with urllib.request.urlopen(f"{base}/api/v1/attention") as response:
                attention = json.load(response)
                self.assertIn("items", attention)
                self.assertIsInstance(attention["items"], list)

            with urllib.request.urlopen(f"{base}/api/v1/backtests") as response:
                backtests = json.load(response)
                self.assertEqual(backtests["page"], "backtests")
                self.assertIn("statistics", backtests)
                self.assertIsInstance(backtests["rows"], list)

            with urllib.request.urlopen(f"{base}/api/v1/experiments/EXP-1") as response:
                experiment = json.load(response)
                self.assertEqual(experiment["experiment"]["id"], "EXP-1")
                self.assertIn("hypothesis", experiment["experiment"])

            with urllib.request.urlopen(f"{base}/api/v1/work-items/JOB-1") as response:
                detail = json.load(response)
                self.assertEqual(detail["work_item"]["id"], "JOB-1")
                self.assertNotIn("payload_json", json.dumps(detail["events"]))

            with urllib.request.urlopen(f"{base}/api/events?limit=1") as response:
                events = json.load(response)["events"]
                self.assertEqual(len(events), 1)
                self.assertNotIn("payload_json", events[0])

            with urllib.request.urlopen(f"{base}/api/snapshot?limit=1") as response:
                snapshot = json.load(response)
                self.assertIn("health", snapshot)
                self.assertIn("summary", snapshot)
                self.assertEqual(len(snapshot["events"]), 1)

            request = urllib.request.Request(f"{base}/api/summary", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request)
            context.exception.close()
            self.assertEqual(context.exception.code, 405)
            self.assertEqual(context.exception.headers["Allow"], "GET")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_loopback_control_requires_confirmation_and_records_pause(self) -> None:
        service = ControlService(self.database, Path(self.temporary.name))
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.database, control_service=service),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(
                f"{base}/api/v1/control/pause", method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request)
            context.exception.close()
            self.assertEqual(context.exception.code, 428)
            self.assertEqual(
                self.database.control_status()["desired_state"], "running",
            )

            request = urllib.request.Request(
                f"{base}/api/v1/control/pause",
                headers={"X-ATS-Lab-Confirm": "pause"},
                method="POST",
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(result["action"], "pause")
            self.assertEqual(result["control"]["desired_state"], "paused")
            self.assertEqual(
                self.database.control_status()["updated_by"], "loop:pause",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_optional_static_control_room_is_same_origin_and_blocks_traversal(self) -> None:
        frontend = Path(self.temporary.name) / "frontend"
        frontend.mkdir()
        (frontend / "index.html").write_text("<h1>Control Room</h1>")
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(self.database, static_dir=frontend),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(f"{base}/") as response:
                self.assertEqual(response.read(), b"<h1>Control Room</h1>")
                self.assertEqual(response.headers.get_content_type(), "text/html")
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(f"{base}/../lab.sqlite3")
            context.exception.close()
            self.assertEqual(context.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_local_command_endpoint_is_allowlisted_and_confirmed(self) -> None:
        class FakeRunner:
            def run(self, action: str) -> dict[str, object]:
                if action != "status":
                    raise LocalCommandError("unexpected action")
                return {
                    "action": action, "argv": ["python", "-m", "ats_lab.cli", "status"],
                    "exit_code": 0, "timed_out": False, "output": "{\"healthy\":true}",
                    "truncated": False,
                }

        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.database, command_runner=FakeRunner()),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(
                f"{base}/api/v1/commands/status", method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request)
            context.exception.close()
            self.assertEqual(context.exception.code, 428)

            request = urllib.request.Request(
                f"{base}/api/v1/commands/status",
                headers={"X-ATS-Lab-Confirm": "command"}, method="POST",
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
            self.assertEqual(result["action"], "status")
            self.assertEqual(result["exit_code"], 0)

            request = urllib.request.Request(
                f"{base}/api/v1/commands/status;env",
                headers={"X-ATS-Lab-Confirm": "command"}, method="POST",
            )
            with self.assertRaises(urllib.error.HTTPError) as context:
                urllib.request.urlopen(request)
            context.exception.close()
            self.assertEqual(context.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


    def test_hpo_study_detail_route_decodes_percent_encoded_ids(self) -> None:
        self.database.add_evaluation(Evaluation(
            experiment_id="EXP-1", verdict=Verdict.HPO_CANDIDATE,
            summary="promising", next_step="run OOS", evaluator="test",
        ))
        study_id = self.database.schedule_hpo_candidate("EXP-1", "JOB-1")["id"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.database))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            encoded = urllib.parse.quote(study_id, safe="")
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server.server_port}/api/v1/hpo/studies/{encoded}"
            ) as response:
                payload = json.load(response)

            self.assertEqual(payload["study_id"], study_id)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class WebApiHostHeaderGateTests(unittest.TestCase):
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

    def test_loopback_host_headers_with_ports_are_accepted(self) -> None:
        base = self._serve()
        port = base.rsplit(":", 1)[-1]

        with urllib.request.urlopen(f"{base}/api/health") as response:
            self.assertEqual(response.status, 200)
        request = urllib.request.Request(
            f"{base}/api/health", headers={"Host": f"localhost:{port}"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)

    def test_foreign_host_header_is_rejected_with_403(self) -> None:
        base = self._serve()

        request = urllib.request.Request(
            f"{base}/api/health", headers={"Host": "attacker.example"},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(
            json.load(error.exception)["error"]["code"], "forbidden_host",
        )

    def test_post_rejects_foreign_host_before_confirmation_gate(self) -> None:
        base = self._serve()

        request = urllib.request.Request(
            f"{base}/api/v1/control/pause",
            data=b"{}", method="POST",
            headers={
                "Host": "attacker.example",
                "X-ATS-Lab-Confirm": "pause",
            },
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)

    def test_explicit_non_loopback_bind_accepts_literal_host_only(self) -> None:
        base = self._serve(bound_host="192.168.10.10")

        request = urllib.request.Request(
            f"{base}/api/health", headers={"Host": "192.168.10.10:8766"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 200)
        request = urllib.request.Request(
            f"{base}/api/health", headers={"Host": "attacker.example"},
        )
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        self.assertEqual(error.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
