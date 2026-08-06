from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.supervisor import BatchSupervisor
from ats_lab.worker import DispatchResult


class BlockingDispatcher:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def dispatch(self, request: dict) -> DispatchResult:
        self.started.set()
        self.release.wait(timeout=2)
        return DispatchResult(outcome="retry", detail="test complete")


class RuntimeHeartbeatTests(unittest.TestCase):
    def test_dispatch_heartbeat_refreshes_during_long_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "workflow.sqlite3")
            database.initialize()
            dispatcher = BlockingDispatcher()
            supervisor = BatchSupervisor(
                database,
                dispatcher,
                "heartbeat-worker",
                heartbeat_interval_seconds=0.02,
            )
            supervisor._runtime("executing", batch_id="BATCH-1")
            before = database.supervisor_runtime_status()["heartbeat_at"]

            call = threading.Thread(
                target=lambda: supervisor._dispatch({"task_type": "execute_batch"}),
                daemon=True,
            )
            call.start()
            self.assertTrue(dispatcher.started.wait(timeout=1))
            time.sleep(0.08)
            during = database.supervisor_runtime_status()
            dispatcher.release.set()
            call.join(timeout=1)

            self.assertNotEqual(before, during["heartbeat_at"])
            self.assertEqual(during["phase"], "executing")
            self.assertFalse(call.is_alive())


if __name__ == "__main__":
    unittest.main()
