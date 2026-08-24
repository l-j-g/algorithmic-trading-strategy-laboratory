from __future__ import annotations

import json
import subprocess
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ats_lab.stack_preflight import StackPreflight, StackPreflightError


class StackPreflightTests(unittest.TestCase):
    def test_all_required_checks_pass_without_secrets(self) -> None:
        seen = []

        def run(command, **kwargs):
            seen.append(command)
            stdout = "ok"
            if "inspect" in command:
                stdout = "true"
            elif "pg_isready" in command:
                stdout = "accepting connections"
            elif "SELECT 1" in command[-1]:
                stdout = "1"
            elif "pg_catalog.pg_tables" in command[-1]:
                stdout = "backtestsession\ncandle\nsignificancetestsession\n"
            elif "EXISTS (SELECT * FROM candle)" in command[-1]:
                stdout = "t"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        def probe(name, url, kind):
            seen.append((name, url, kind))
            return {"name": name, "status": "healthy", "url": url}

        result = StackPreflight(command_runner=run, endpoint_probe=probe).check()

        self.assertTrue(result["healthy"])
        self.assertEqual(seen[0], ["docker", "info", "--format", "{{.ServerVersion}}"])
        self.assertEqual(
            [check["name"] for check in result["checks"]],
            [
                "docker_daemon", "jesse_postgres_container",
                "jesse_postgres_ready", "jesse_postgres_read_only",
                "jesse_postgres_tables", "jesse_candle_data", "jesse_dashboard",
                "jesse_mcp", "memory_api",
            ],
        )
        self.assertEqual(seen[1][:3], ["docker", "inspect", "--format"])
        self.assertIn("pg_isready", seen[2])
        self.assertIn("BEGIN TRANSACTION READ ONLY", seen[3][-1])
        self.assertNotIn("exchangeapikeys", seen[3][-1].lower())
        self.assertIn("pg_catalog.pg_tables", seen[4][-1])
        self.assertNotIn("exchangeapikeys", seen[4][-1].lower())
        self.assertNotIn("token", str(result).lower())
        self.assertNotIn("password", str(result).lower())

    def test_stopped_infrastructure_is_precise_fail_closed_blocker(self) -> None:
        def run(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="Cannot connect to Docker daemon",
            )

        result = StackPreflight(command_runner=run).check()
        self.assertFalse(result["healthy"])
        self.assertEqual(result["blocker_code"], "infrastructure_preflight_failed")
        self.assertEqual(result["failed_check"], "docker_daemon")
        with self.assertRaises(StackPreflightError):
            StackPreflight(command_runner=run).require_healthy()

    def test_postgres_table_failure_stops_before_http_checks(self) -> None:
        seen = []

        def run(command, **kwargs):
            seen.append(command)
            stdout = "ok"
            if "inspect" in command:
                stdout = "true"
            elif "pg_isready" in command:
                stdout = "accepting connections"
            elif "SELECT 1" in command[-1]:
                stdout = "1"
            elif "pg_catalog.pg_tables" in command[-1]:
                stdout = "backtestsession\ncandle\n"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        def probe(name, url, kind):
            self.fail(f"HTTP probe ran after failed PostgreSQL check: {name}")

        result = StackPreflight(command_runner=run, endpoint_probe=probe).check()

        self.assertFalse(result["healthy"])
        self.assertEqual(result["blocker_code"], "infrastructure_preflight_failed")
        self.assertEqual(result["failed_check"], "jesse_postgres_tables")
        self.assertEqual(
            [check["name"] for check in result["checks"]][-1],
            "jesse_postgres_tables",
        )

    def test_postgres_identity_is_configurable_without_shell_or_secret(self) -> None:
        seen = []

        def run(command, **kwargs):
            seen.append(command)
            stdout = "true" if "inspect" in command else "ok"
            if "pg_isready" in command:
                stdout = "accepting connections"
            if "SELECT 1" in command[-1]:
                stdout = "1"
            if "pg_catalog.pg_tables" in command[-1]:
                stdout = "backtestsession\ncandle\nsignificancetestsession\n"
            if "EXISTS (SELECT * FROM candle)" in command[-1]:
                stdout = "t"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        result = StackPreflight(
            command_runner=run, endpoint_probe=lambda name, url, kind: {
                "name": name, "status": "healthy", "url": url,
            }, postgres_container="jesse-db", postgres_user="ats_reader",
            postgres_database="jesse_readonly",
        ).check()

        self.assertTrue(result["healthy"])
        flattened = [part for command in seen for part in command]
        self.assertIn("jesse-db", flattened)
        self.assertIn("ats_reader", flattened)
        self.assertIn("jesse_readonly", flattened)
        self.assertFalse(any("shell" in key for key in flattened))

    def test_extra_public_tables_warn_without_failing_preflight(self) -> None:
        def run(command, **kwargs):
            stdout = "ok"
            if "inspect" in command:
                stdout = "true"
            elif "pg_isready" in command:
                stdout = "accepting connections"
            elif "SELECT 1" in command[-1]:
                stdout = "1"
            elif "pg_catalog.pg_tables" in command[-1]:
                stdout = (
                    "backtestsession\ncandle\nsignificancetestsession\n"
                    "strategy_notes\n"
                )
            elif "EXISTS (SELECT * FROM candle)" in command[-1]:
                stdout = "t"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        result = StackPreflight(
            command_runner=run,
            endpoint_probe=lambda name, url, kind: {
                "name": name, "status": "healthy", "url": url,
            },
        ).check()

        self.assertTrue(result["healthy"])
        tables = next(
            check for check in result["checks"]
            if check["name"] == "jesse_postgres_tables"
        )
        self.assertEqual(tables["status"], "healthy")
        self.assertIn("strategy_notes", tables["detail"])
        self.assertIn("advisory", tables["detail"])

    def test_missing_required_table_still_fails_preflight(self) -> None:
        def run(command, **kwargs):
            stdout = "ok"
            if "inspect" in command:
                stdout = "true"
            elif "pg_isready" in command:
                stdout = "accepting connections"
            elif "SELECT 1" in command[-1]:
                stdout = "1"
            elif "pg_catalog.pg_tables" in command[-1]:
                stdout = "candle\nsignificancetestsession\nextra_table\n"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        result = StackPreflight(command_runner=run).check()

        self.assertFalse(result["healthy"])
        self.assertEqual(result["failed_check"], "jesse_postgres_tables")

    def test_memory_outage_is_reported_but_does_not_block_canonical_work(self) -> None:
        def run(command, **_kwargs):
            stdout = "ok"
            if "inspect" in command:
                stdout = "true"
            elif "pg_isready" in command:
                stdout = "accepting connections"
            elif "SELECT 1" in command[-1]:
                stdout = "1"
            elif "pg_catalog.pg_tables" in command[-1]:
                stdout = "backtestsession\ncandle\nsignificancetestsession\n"
            elif "EXISTS (SELECT * FROM candle)" in command[-1]:
                stdout = "t"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        def probe(name, url, _kind):
            return {
                "name": name,
                "status": "failed" if name == "memory_api" else "healthy",
                "url": url,
            }

        result = StackPreflight(command_runner=run, endpoint_probe=probe).check()
        self.assertTrue(result["healthy"])
        self.assertTrue(result["memory_degraded"])
        self.assertEqual(result["degraded_checks"], ["memory_api"])

    def test_empty_candle_table_fails_preflight(self) -> None:
        def run(command, **kwargs):
            stdout = "ok"
            if "inspect" in command:
                stdout = "true"
            elif "pg_isready" in command:
                stdout = "accepting connections"
            elif "SELECT 1" in command[-1]:
                stdout = "1"
            elif "pg_catalog.pg_tables" in command[-1]:
                stdout = "backtestsession\ncandle\nsignificancetestsession\n"
            elif "EXISTS (SELECT * FROM candle)" in command[-1]:
                stdout = "f"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        result = StackPreflight(command_runner=run).check()

        self.assertFalse(result["healthy"])
        self.assertEqual(result["failed_check"], "jesse_candle_data")


class FakeMcpProbeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        server = self.server
        body = server.response_body  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if server.session_id:  # type: ignore[attr-defined]
            self.send_header("mcp-session-id", server.session_id)  # type: ignore[attr-defined]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        server = self.server
        server.deleted_sessions.append(  # type: ignore[attr-defined]
            self.headers.get("mcp-session-id"),
        )
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class FakeMcpProbeServer:
    def __init__(self, response_body: bytes, session_id: str | None) -> None:
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcpProbeHandler)
        self.http.response_body = response_body  # type: ignore[attr-defined]
        self.http.session_id = session_id  # type: ignore[attr-defined]
        self.http.deleted_sessions = []  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self) -> FakeMcpProbeServer:
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()

    @property
    def url(self) -> str:
        host, port = self.http.server_address
        return f"http://{host}:{port}/mcp"


class McpProbeTests(unittest.TestCase):
    def probe_mcp_endpoint(self, server: FakeMcpProbeServer) -> dict:
        def run(command, **kwargs):
            stdout = "ok"
            if "inspect" in command:
                stdout = "true"
            elif "pg_isready" in command:
                stdout = "accepting connections"
            elif "SELECT 1" in command[-1]:
                stdout = "1"
            elif "pg_catalog.pg_tables" in command[-1]:
                stdout = "backtestsession\ncandle\nsignificancetestsession\n"
            elif "EXISTS (SELECT * FROM candle)" in command[-1]:
                stdout = "t"
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        preflight = StackPreflight(
            dashboard_url="http://127.0.0.1:1",
            mcp_url=server.url,
            memory_health_url="http://127.0.0.1:1/health",
            command_runner=run,
        )
        try:
            return preflight.endpoint_probe("jesse_mcp", server.url, "mcp")
        except (OSError, TimeoutError, urllib.error.URLError, ValueError) as error:
            return {
                "name": "jesse_mcp", "status": "failed",
                "detail": type(error).__name__,
            }

    def test_mcp_probe_accepts_jsonrpc_sse_result_and_tears_down_session(self) -> None:
        body = (
            "event: message\n"
            "data: " + json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "result": {"protocolVersion": "2024-11-05"},
            }) + "\n\n"
        ).encode()
        with FakeMcpProbeServer(body, "probe-session-1") as server:
            check = self.probe_mcp_endpoint(server)
            self.assertEqual(check["status"], "healthy")
            self.assertEqual(server.http.deleted_sessions, ["probe-session-1"])  # type: ignore[attr-defined]

    def test_mcp_probe_rejects_non_jsonrpc_200_body(self) -> None:
        with FakeMcpProbeServer(b"<html>ok</html>", None) as server:
            check = self.probe_mcp_endpoint(server)
        self.assertEqual(check["status"], "failed")
        self.assertEqual(check["detail"], "ValueError")

    def test_mcp_probe_rejects_jsonrpc_error_envelope(self) -> None:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32000, "message": "nope"},
        }).encode()
        with FakeMcpProbeServer(body, None) as server:
            check = self.probe_mcp_endpoint(server)
        self.assertEqual(check["status"], "failed")
