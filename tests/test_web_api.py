from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from http.server import ThreadingHTTPServer

from ats_lab.database import WorkflowDatabase
from ats_lab.models import ExperimentSpec, WorkItem, WorkState
from ats_lab.web_api import ReadOnlyApi, make_handler


class WebApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = WorkflowDatabase(Path(self.temporary.name) / "lab.sqlite3")
        self.database.initialize()
        self.database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="SafeStrategy",
        ))
        self.database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1,
            state=WorkState.READY,
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


if __name__ == "__main__":
    unittest.main()
