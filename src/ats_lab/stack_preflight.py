"""Deterministic local infrastructure checks for ATS execution."""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable


EXPECTED_PUBLIC_TABLES = frozenset({
    "candle", "backtestsession", "significancetestsession",
})


class PublicTablesCheck:
    """Required-tables subset validation that records unknown extras."""

    def __init__(self) -> None:
        self.observed: set[str] = set()

    def validate(self, output: str) -> bool:
        self.observed.update(line for line in output.splitlines() if line)
        return EXPECTED_PUBLIC_TABLES <= self.observed

    def unexpected(self) -> list[str]:
        return sorted(self.observed - EXPECTED_PUBLIC_TABLES)


class StackPreflightError(RuntimeError):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(str(result.get("detail") or "infrastructure preflight failed"))
        self.result = result


class StackPreflight:
    def __init__(
        self, *, dashboard_url: str = "http://127.0.0.1:9000",
        mcp_url: str = "http://127.0.0.1:9002/mcp",
        memory_health_url: str = "http://127.0.0.1:18000/health",
        timeout_seconds: float = 3,
        postgres_container: str = "postgres",
        postgres_user: str = "jesse_user",
        postgres_database: str = "jesse_db",
        command_runner: Callable[..., Any] = subprocess.run,
        endpoint_probe: Callable[[str, str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.dashboard_url = dashboard_url
        self.mcp_url = mcp_url
        self.memory_health_url = memory_health_url
        self.timeout_seconds = timeout_seconds
        self.postgres_container = postgres_container
        self.postgres_user = postgres_user
        self.postgres_database = postgres_database
        self.command_runner = command_runner
        self.endpoint_probe = endpoint_probe or self._probe_endpoint

    def check(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        try:
            result = self.command_runner(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, text=True, timeout=self.timeout_seconds,
                check=False,
            )
            healthy = result.returncode == 0 and bool(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            healthy = False
        checks.append({"name": "docker_daemon", "status": "healthy" if healthy else "failed"})
        if not healthy:
            return self._failure(
                checks, "docker_daemon",
                "Docker daemon unavailable; start Docker Desktop before ATS supervisor",
            )
        tables = PublicTablesCheck()
        postgres_checks = (
            (
                "jesse_postgres_container",
                ["docker", "inspect", "--format", "{{.State.Running}}",
                 self.postgres_container],
                lambda output: output == "true",
                "Jesse PostgreSQL container is not running",
            ),
            (
                "jesse_postgres_ready",
                self._postgres_exec("pg_isready", "-U", self.postgres_user,
                                    "-d", self.postgres_database),
                lambda output: "accepting connections" in output,
                "Jesse PostgreSQL is not accepting connections",
            ),
            (
                "jesse_postgres_read_only",
                self._psql_command("BEGIN TRANSACTION READ ONLY; SELECT 1;"),
                lambda output: "1" in output.splitlines(),
                "Jesse PostgreSQL read-only SELECT 1 failed",
            ),
            (
                "jesse_postgres_tables",
                self._psql_command(
                    "SELECT tablename FROM pg_catalog.pg_tables "
                    "WHERE schemaname='public' ORDER BY tablename;"
                ),
                tables.validate,
                "Jesse PostgreSQL missing expected public tables",
            ),
        )
        for name, command, validate, detail in postgres_checks:
            healthy = self._command_healthy(command, validate)
            entry: dict[str, Any] = {
                "name": name, "status": "healthy" if healthy else "failed",
            }
            if name == "jesse_postgres_tables" and healthy:
                extras = tables.unexpected()
                if extras:
                    entry["detail"] = (
                        "warning: unexpected public tables (advisory): "
                        + ", ".join(extras)
                    )
            checks.append(entry)
            if not healthy:
                return self._failure(checks, name, detail)
        for name, url, kind in (
            ("jesse_dashboard", self.dashboard_url, "http"),
            ("jesse_mcp", self.mcp_url, "mcp"),
            ("memory_api", self.memory_health_url, "health"),
        ):
            try:
                check = self.endpoint_probe(name, url, kind)
            except (OSError, TimeoutError, urllib.error.URLError, ValueError) as error:
                check = {"name": name, "status": "failed", "url": url,
                         "detail": type(error).__name__}
            checks.append(check)
            if check.get("status") != "healthy":
                if name == "memory_api":
                    check["status"] = "degraded"
                    check["detail"] = "advisory memory unavailable; SQLite-only mode"
                    return {
                        "healthy": True,
                        "memory_degraded": True,
                        "degraded_checks": ["memory_api"],
                        "checks": checks,
                    }
                return self._failure(
                    checks, name, f"{name} unavailable at {url}",
                )
        return {"healthy": True, "memory_degraded": False, "checks": checks}

    def _postgres_exec(self, *command: str) -> list[str]:
        return ["docker", "exec", self.postgres_container, *command]

    def _psql_command(self, sql: str) -> list[str]:
        return self._postgres_exec(
            "psql", "-U", self.postgres_user, "-d", self.postgres_database,
            "-v", "ON_ERROR_STOP=1", "-Atc", sql,
        )

    def _command_healthy(
        self, command: list[str], validate: Callable[[str], bool],
    ) -> bool:
        try:
            result = self.command_runner(
                command, capture_output=True, text=True,
                timeout=self.timeout_seconds, check=False,
            )
            return result.returncode == 0 and validate(result.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            return False

    def require_healthy(self) -> dict[str, Any]:
        result = self.check()
        if not result["healthy"]:
            raise StackPreflightError(result)
        return result

    @staticmethod
    def _failure(checks: list[dict[str, Any]], name: str, detail: str) -> dict[str, Any]:
        return {
            "healthy": False,
            "blocker_code": "infrastructure_preflight_failed",
            "failed_check": name,
            "detail": detail,
            "checks": checks,
        }

    def _probe_endpoint(self, name: str, url: str, kind: str) -> dict[str, Any]:
        data = None
        headers = {"Accept": "application/json"}
        if kind == "mcp":
            data = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "ats-lab-preflight", "version": "1"}},
            }).encode()
            headers.update({
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            })
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read(65536)
            if response.status >= 400:
                raise ValueError(f"HTTP {response.status}")
        if kind == "health":
            payload = json.loads(body.decode()) if body.strip() else {}
            if isinstance(payload, dict) and payload.get("status") in {
                "failed", "error", "unhealthy",
            }:
                raise ValueError("unhealthy response")
        return {"name": name, "status": "healthy", "url": url}
