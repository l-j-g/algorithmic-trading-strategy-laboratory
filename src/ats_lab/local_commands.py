"""Local-only, allowlisted command execution for the Control Room.

This module deliberately accepts action names, not command strings.  It is
safe for a later loopback web adapter to call without exposing a shell.
"""
from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, TypedDict


DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_OUTPUT_LIMIT_BYTES = 256 * 1024
MAX_TIMEOUT_SECONDS = 300.0
MAX_OUTPUT_LIMIT_BYTES = 4 * 1024 * 1024

ALLOWED_ACTIONS = frozenset({
    "status",
    "preflight",
    "hpo_doctor",
    "supervisor_plan",
    "recover_claims_preview",
})

# Values are fixed command arguments.  Repo/database paths are supplied by the
# trusted local runner, never by a browser request or command-string parser.
_ACTION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "status": ("status", "--format", "json"),
    "preflight": ("preflight",),
    "hpo_doctor": ("hpo", "--doctor", "--format", "json"),
    "supervisor_plan": ("supervisor", "--plan"),
    "recover_claims_preview": ("recover-claims",),
}

# Fixed system search path.  Do not inherit PATH, proxy, cloud, token, or
# application-specific variables from the process hosting the web server.
SAFE_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class LocalCommandResult(TypedDict):
    action: str
    argv: list[str]
    exit_code: int | None
    timed_out: bool
    output: str
    truncated: bool


class LocalCommandError(ValueError):
    """Raised when caller input is outside fixed local command policy."""


def _validate_action(action: str) -> None:
    if not isinstance(action, str):
        raise LocalCommandError("action must be a string")
    if action not in ALLOWED_ACTIONS:
        raise LocalCommandError(f"unknown local command action: {action!r}")


def _execute_process(
    argv: list[str], *, cwd: Path, env: dict[str, str],
    timeout_seconds: float, output_limit_bytes: int,
) -> tuple[int | None, bool, str, bool]:
    """Run one argv list with bounded combined stdout/stderr capture."""
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    captured = bytearray()
    truncated = False
    timed_out = False
    drain_deadline: float | None = None

    def terminate() -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        except OSError:
            process.kill()

    deadline = time.monotonic() + timeout_seconds
    try:
        while selector.get_map():
            now = time.monotonic()
            if not timed_out and now >= deadline:
                timed_out = True
                terminate()
                drain_deadline = now + 0.5

            if timed_out:
                remaining = max(0.0, (drain_deadline or now) - now)
                if remaining <= 0:
                    break
                wait_seconds = min(remaining, 0.05)
            else:
                wait_seconds = min(max(0.0, deadline - now), 0.1)

            for key, _ in selector.select(wait_seconds):
                try:
                    chunk = os.read(key.fd, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining_bytes = output_limit_bytes - len(captured)
                if remaining_bytes > 0:
                    captured.extend(chunk[:remaining_bytes])
                if len(chunk) > max(0, remaining_bytes):
                    # Continue draining pipe, but retain no further bytes.
                    output_truncated = True
                else:
                    output_truncated = False
                if output_truncated:
                    truncated = True
    finally:
        selector.close()

    if timed_out and process.poll() is None:
        terminate()
    try:
        exit_code = process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        terminate()
        exit_code = process.wait(timeout=1.0)
    process.stdout.close()
    return exit_code, timed_out, captured.decode("utf-8", errors="replace"), truncated


class LocalCommandRunner:
    """Execute fixed ATS Lab inspection/recovery-preview actions locally."""

    def __init__(
        self,
        repo: Path,
        *,
        python_executable: str | Path | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    ) -> None:
        resolved_repo = Path(repo).expanduser().resolve()
        if not resolved_repo.is_dir():
            raise LocalCommandError("repo must be an existing directory")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0 < timeout_seconds <= MAX_TIMEOUT_SECONDS
        ):
            raise LocalCommandError(
                f"timeout_seconds must be between 0 and {MAX_TIMEOUT_SECONDS:g}"
            )
        if (
            isinstance(output_limit_bytes, bool)
            or not isinstance(output_limit_bytes, int)
            or not 0 < output_limit_bytes <= MAX_OUTPUT_LIMIT_BYTES
        ):
            raise LocalCommandError(
                f"output_limit_bytes must be between 0 and {MAX_OUTPUT_LIMIT_BYTES}"
            )
        self.repo = resolved_repo
        self.python_executable = str(
            Path(python_executable or sys.executable).expanduser().resolve()
        )
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes

    def argv_for(self, action: str) -> list[str]:
        _validate_action(action)
        return [
            self.python_executable,
            "-m",
            "ats_lab.cli",
            "--repo",
            str(self.repo),
            "--database",
            str(self.repo / ".ats-lab" / "laboratory.sqlite3"),
            *_ACTION_ARGUMENTS[action],
        ]

    def sanitized_environment(self) -> dict[str, str]:
        """Return fixed non-secret environment needed by ATS Lab subprocesses."""
        return {
            "PATH": SAFE_PATH,
            "PYTHONPATH": str(self.repo / "src"),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "LC_ALL": "C",
            "LANG": "C",
        }

    def run(self, action: str) -> LocalCommandResult:
        _validate_action(action)
        argv = self.argv_for(action)
        try:
            exit_code, timed_out, output, truncated = _execute_process(
                argv,
                cwd=self.repo,
                env=self.sanitized_environment(),
                timeout_seconds=self.timeout_seconds,
                output_limit_bytes=self.output_limit_bytes,
            )
        except OSError as error:
            return {
                "action": action,
                "argv": argv,
                "exit_code": None,
                "timed_out": False,
                "output": f"{type(error).__name__}: {error}",
                "truncated": False,
            }
        return {
            "action": action,
            "argv": argv,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output": output,
            "truncated": truncated,
        }


def run_local_command(action: str, repo: Path, **kwargs: Any) -> LocalCommandResult:
    """Convenience wrapper for future web/API integration."""
    if kwargs.keys() - {"python_executable", "timeout_seconds", "output_limit_bytes"}:
        raise LocalCommandError("unsupported local command input")
    return LocalCommandRunner(repo, **kwargs).run(action)
