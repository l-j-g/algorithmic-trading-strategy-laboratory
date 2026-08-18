"""Start, pause, stop, and inspect the canonical supervisor process."""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from .database import WorkflowDatabase


class LoopLauncher(Protocol):
    def launch(self, command: list[str], *, cwd: Path, log_path: Path) -> int: ...


class SubprocessLoopLauncher:
    def launch(self, command: list[str], *, cwd: Path, log_path: Path) -> int:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as output:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        return process.pid


def process_is_alive(process_id: int) -> bool:
    if process_id <= 0:
        return False
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass(frozen=True)
class LoopStatus:
    state: str
    process_id: int | None
    phase: str
    control: str
    repaired_retry_schedules: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "process_id": self.process_id,
            "phase": self.phase,
            "control": self.control,
            "repaired_retry_schedules": self.repaired_retry_schedules,
        }


class LoopControl(Protocol):
    def status(self) -> LoopStatus: ...
    def start(self) -> LoopStatus: ...
    def pause(self) -> LoopStatus: ...
    def stop(self) -> LoopStatus: ...


class SupervisorLoopControl:
    """Process lifecycle boundary shared by CLI and terminal UI."""

    def __init__(
        self,
        database: WorkflowDatabase,
        repo: Path,
        *,
        launcher: LoopLauncher | None = None,
        alive: Callable[[int], bool] = process_is_alive,
        python_executable: str = sys.executable,
        idle_sleep: float = 30.0,
        retry_delay: float = 60.0,
    ) -> None:
        self.database = database
        self.repo = repo.resolve()
        self.launcher = launcher or SubprocessLoopLauncher()
        self.alive = alive
        self.python_executable = python_executable
        self.idle_sleep = idle_sleep
        self.retry_delay = retry_delay

    def _runtime(self) -> tuple[dict | None, bool]:
        runtime = self.database.supervisor_runtime_status()
        process_id = int(runtime["process_id"]) if runtime else 0
        running = bool(
            runtime
            and runtime.get("phase") != "stopped"
            and self.alive(process_id)
        )
        return runtime, running

    def status(self) -> LoopStatus:
        runtime, running = self._runtime()
        control = self.database.control_status()["desired_state"]
        phase = (
            str(runtime.get("phase") or "not_reported")
            if runtime and running else "stopped"
        )
        return LoopStatus(
            state="running" if running else "stopped",
            process_id=int(runtime["process_id"]) if runtime else None,
            phase=phase,
            control=str(control),
        )

    def start(self) -> LoopStatus:
        repaired = self.database.repair_relative_retry_schedules()
        runtime, running = self._runtime()
        previous_control = str(
            self.database.control_status().get("desired_state") or "paused"
        )
        self.database.set_control_state("running", updated_by="loop:start")
        if running:
            current = self.status()
            return replace(
                current,
                state="already_running",
                repaired_retry_schedules=repaired,
            )
        command = [
            self.python_executable,
            "-m", "ats_lab.cli",
            "--repo", str(self.repo),
            "--database", str(self.database.path.resolve()),
            "supervisor", "--continuous",
            "--idle-sleep", str(self.idle_sleep),
            "--retry-delay", str(self.retry_delay),
        ]
        try:
            process_id = self.launcher.launch(
                command,
                cwd=self.repo,
                log_path=self.database.path.parent / "supervisor.log",
            )
        except Exception:
            failure_control = (
                previous_control if previous_control != "running" else "paused"
            )
            self.database.set_control_state(
                failure_control, updated_by="loop:start_failed",
            )
            raise
        return LoopStatus(
            state="started",
            process_id=process_id,
            phase="starting",
            control="running",
            repaired_retry_schedules=repaired,
        )

    def pause(self) -> LoopStatus:
        self.database.set_control_state("paused", updated_by="loop:pause")
        current = self.status()
        return replace(current, state="paused")

    def stop(self) -> LoopStatus:
        self.database.set_control_state(
            "stop_requested", updated_by="loop:stop",
        )
        current = self.status()
        return replace(current, state="stop_requested")
