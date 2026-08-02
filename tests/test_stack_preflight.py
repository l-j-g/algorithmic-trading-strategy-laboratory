from __future__ import annotations

import subprocess
import unittest

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
                "jesse_postgres_tables", "jesse_dashboard", "jesse_mcp",
                "memory_api",
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
