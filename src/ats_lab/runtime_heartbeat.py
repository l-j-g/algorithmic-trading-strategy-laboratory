"""Durable supervisor heartbeat while an external dispatch is in flight."""
from __future__ import annotations

import os
from threading import Event, Thread
from typing import Any

from .database import WorkflowDatabase


class RuntimeHeartbeat:
    """Refresh the supervisor runtime row until a bounded dispatch returns."""

    def __init__(
        self,
        database: WorkflowDatabase,
        *,
        worker_id: str,
        started_at: str,
        phase: str,
        batch_id: str | None,
        detail: dict[str, Any] | None,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.database = database
        self.worker_id = worker_id
        self.started_at = started_at
        self.phase = phase
        self.batch_id = batch_id
        self.detail = detail
        self.interval_seconds = interval_seconds
        self._stop = Event()
        self._thread: Thread | None = None

    def __enter__(self) -> "RuntimeHeartbeat":
        self._pulse()
        self._thread = Thread(
            target=self._run,
            name="ats-lab-supervisor-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self.interval_seconds * 2, 1.0))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self._pulse()
            except Exception:
                # Heartbeat must never interrupt or alter external execution.
                continue

    def _pulse(self) -> None:
        self.database.update_supervisor_runtime(
            worker_id=self.worker_id,
            process_id=os.getpid(),
            phase=self.phase,
            batch_id=self.batch_id,
            detail=self.detail,
            started_at=self.started_at,
        )
