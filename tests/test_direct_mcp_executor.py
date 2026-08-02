from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from ats_lab.database import WorkflowDatabase
from ats_lab.direct_mcp_executor import (
    DashboardClient,
    DirectExecutionConfig,
    DirectMcpDispatcher,
    McpClient,
    load_direct_execution_config,
)
from ats_lab.models import ExperimentSpec, WorkItem, WorkState
from ats_lab.worker import DispatchResult


ROUTES = [
    {
        "exchange": "Binance Perpetual Futures",
        "symbol": "BTC-USDT",
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "finish_date": "2024-03-31",
    },
    {
        "exchange": "Binance Perpetual Futures",
        "symbol": "ETH-USDT",
        "timeframe": "1h",
        "start_date": "2024-01-01",
        "finish_date": "2024-03-31",
    },
]


class FakeMcpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.server.requests.append(payload)  # type: ignore[attr-defined]
        method = payload["method"]
        if method != "initialize":
            assert self.headers.get("mcp-session-id") == "mcp-test-session"
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "fake-jesse", "version": "1"},
            }
        elif method == "notifications/initialized":
            self._reply(None, notification=True)
            return
        elif method == "tools/call":
            params = payload["params"]
            name = params["name"]
            arguments = params["arguments"]
            self.server.tool_calls.append((name, arguments))  # type: ignore[attr-defined]
            if name == "create_backtest_draft":
                result = {"backtest_id": "jesse-session-1"}
            elif name == "run_backtest":
                self.server.run_calls += 1  # type: ignore[attr-defined]
                result = {"status": "started", "backtest_id": arguments["session_id"]}
            elif name == "get_backtest_session":
                poll = self.server.poll_count  # type: ignore[attr-defined]
                self.server.poll_count += 1  # type: ignore[attr-defined]
                statuses = self.server.statuses  # type: ignore[attr-defined]
                status = statuses[min(poll, len(statuses) - 1)]
                session = {
                    "id": arguments["session_id"],
                    "status": status,
                    "state": {"results": {"executing": status == "running"}},
                    "metrics": self.server.metrics if status == "finished" else {},  # type: ignore[attr-defined]
                }
                if status == "stopped":
                    session["exception"] = "mechanical failure"
                result = {"data": {"session": session}, "error": None}
            else:
                result = {"status": "error", "message": f"unexpected tool {name}"}
        else:
            result = {}
        self._reply({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

    def _reply(self, payload: object, notification: bool = False) -> None:
        body = b"" if notification else (
            "event: message\ndata: " + json.dumps(payload) + "\n\n"
        ).encode()
        self.send_response(202 if notification else 200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("mcp-session-id", "mcp-test-session")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeMcpServer:
    def __init__(self, statuses: list[str]) -> None:
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), FakeMcpHandler)
        self.http.requests = []
        self.http.tool_calls = []
        self.http.run_calls = 0
        self.http.poll_count = 0
        self.http.statuses = statuses
        self.http.metrics = {
            "net_profit_percentage": 12.345678901234,
            "route_runs": [
                {"session_id": "route-a", "route": ROUTES[0], "net_profit_percentage": 5.0},
                {"session_id": "route-b", "route": ROUTES[1], "net_profit_percentage": 7.0},
            ],
        }
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self) -> FakeMcpServer:
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


class RecordingFallback:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def dispatch(self, request: dict) -> DispatchResult:
        self.requests.append(request)
        return DispatchResult(
            outcome="finished",
            payload={"outcome": "finished", "prepared_work_item_ids": ["JOB-1"]},
        )


class FakeDashboard:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.started: list[str] = []

    def run_backtest(self, session_id: str) -> object:
        if self.error:
            raise self.error
        self.started.append(session_id)
        return {"status": "started"}


def batch_request(*, change_scope: str = "") -> dict:
    return {
        "schema_version": 1,
        "task_type": "execute_batch",
        "batch_id": "BATCH-1",
        "requests": [{
            "work_item_id": "JOB-1",
            "experiment_id": "EXP-1",
            "experiment": {
                "strategy_name": "ExistingStrategy",
                "routes": ROUTES,
                "change_scope": change_scope,
                "sizing_model": "risk quantity capped at 95% available_margin",
            },
            "work_item": {"operation": "backtest"},
        }],
    }


class DirectMcpExecutorTests(unittest.TestCase):
    def make_dispatcher(
        self,
        root: str,
        server: FakeMcpServer,
        *,
        max_polls: int = 4,
        fallback: RecordingFallback | None = None,
        dashboard: FakeDashboard | None = None,
    ) -> tuple[DirectMcpDispatcher, WorkflowDatabase]:
        database = WorkflowDatabase(Path(root) / "lab.sqlite3")
        database.initialize()
        database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="ExistingStrategy",
        ))
        database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=1,
            state=WorkState.RUNNING, specification={"operation": "backtest"},
        ))
        dispatcher = DirectMcpDispatcher(
            database,
            DirectExecutionConfig(
                enabled=True,
                mcp_url=server.url,
                poll_initial_seconds=0,
                poll_max_seconds=0,
                max_polls=max_polls,
            ),
            fallback=fallback,
            sleep=lambda _seconds: None,
            dashboard_client=dashboard,
        )
        return dispatcher, database

    def test_client_initialize_session_header_and_sse_decode(self) -> None:
        with FakeMcpServer(["finished"]) as server:
            client = McpClient(server.url)
            client.initialize()
            result = client.call_tool(
                "get_backtest_session", {"session_id": "jesse-session-1"}
            )
            self.assertEqual(result["data"]["session"]["status"], "finished")

    def test_create_run_poll_finished_preserves_exact_metrics_and_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(tmp, server)
            result = dispatcher.dispatch(batch_request())
            run = result.payload["results"][0]["evidence"]["run"]
            self.assertEqual(result.outcome, "finished")
            self.assertEqual(run["metrics"], server.http.metrics)
            self.assertEqual(run["raw_result"], {
                "session_id": "jesse-session-1",
                "status": "finished",
                "metrics": server.http.metrics,
            })
            self.assertEqual(len(run["metrics"]["route_runs"]), 2)
            self.assertEqual(server.http.run_calls, 1)
            telemetry = database.rows(
                "SELECT * FROM direct_execution_telemetry ORDER BY id DESC LIMIT 1"
            )[0]
            self.assertEqual(telemetry["model_call_count"], 0)
            self.assertGreater(telemetry["request_bytes"], 0)
            self.assertGreater(telemetry["mcp_call_count"], 0)

    def test_mcp_started_but_draft_uses_dashboard_and_verifies_start(self) -> None:
        dashboard = FakeDashboard()
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["draft", "running", "finished"]
        ) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, dashboard=dashboard)
            result = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(result["outcome"], "finished")
            self.assertEqual(dashboard.started, ["jesse-session-1"])
            self.assertEqual(server.http.run_calls, 1)

    def test_dashboard_unavailable_returns_prompt_start_retry(self) -> None:
        dashboard = FakeDashboard(error=OSError("dashboard offline"))
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["draft"]) as server:
            dispatcher, database = self.make_dispatcher(
                tmp, server, max_polls=20, dashboard=dashboard,
            )
            result = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(result["outcome"], "retry")
            self.assertEqual(result["blocker_code"], "jesse_start_recovery_failed")
            self.assertLessEqual(server.http.poll_count, 2)
            checkpoint = database.rows(
                "SELECT state FROM direct_execution_sessions WHERE work_item_id='JOB-1'"
            )[0]
            self.assertEqual(checkpoint["state"], "start_recovery_failed")

    def test_resume_running_checkpoint_that_is_draft_recovers_once(self) -> None:
        dashboard = FakeDashboard()
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["draft", "running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(tmp, server, dashboard=dashboard)
            request = batch_request()["requests"][0]
            dispatcher._save_checkpoint(
                "JOB-1", "EXP-1", "existing-session",
                dispatcher._fingerprint(request), "running",
            )
            result = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(result["outcome"], "finished")
            self.assertEqual(dashboard.started, [])
            self.assertEqual(server.http.run_calls, 1)
            self.assertFalse(any(
                name == "create_backtest_draft" for name, _ in server.http.tool_calls
            ))

    def test_unfinished_member_does_not_block_later_finished_member(self) -> None:
        class BatchClient:
            def __init__(self, _url: str, _timeout: float) -> None:
                self.call_count = self.request_bytes = self.response_bytes = 0
                self.created = 0

            def initialize(self) -> None:
                return

            def call_tool(self, name: str, arguments: dict | None = None) -> object:
                arguments = arguments or {}
                if name == "create_backtest_draft":
                    self.created += 1
                    return {"backtest_id": f"session-{self.created}"}
                if name == "run_backtest":
                    return {"status": "started"}
                session_id = arguments["session_id"]
                status = "running" if session_id == "session-1" else "finished"
                return {"status": status, "metrics": {} if status == "finished" else {}}

        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            requests = []
            for number in (1, 2):
                experiment_id = f"EXP-{number}"
                work_id = f"JOB-{number}"
                database.upsert_experiment(ExperimentSpec(
                    id=experiment_id, strategy_name="ExistingStrategy",
                ))
                database.upsert_work_item(WorkItem(
                    id=work_id, experiment_id=experiment_id, priority=number,
                    state=WorkState.RUNNING, specification={"operation": "backtest"},
                ))
                item = batch_request()["requests"][0]
                item = {**item, "work_item_id": work_id, "experiment_id": experiment_id}
                requests.append(item)
            client = BatchClient("", 0)
            dispatcher = DirectMcpDispatcher(
                database,
                DirectExecutionConfig(
                    enabled=True, max_polls=1, poll_initial_seconds=0,
                    poll_max_seconds=0,
                ),
                client_factory=lambda _url, _timeout: client,
                sleep=lambda _seconds: None,
            )
            result = dispatcher.dispatch({
                "task_type": "execute_batch", "batch_id": "BATCH-2",
                "requests": requests,
            })
            members = {row["work_item_id"]: row for row in result.payload["results"]}
            self.assertEqual(members["JOB-1"]["outcome"], "retry")
            self.assertEqual(members["JOB-2"]["outcome"], "finished")

    def test_dashboard_config_separates_api_and_display_urls(self) -> None:
        config = DirectExecutionConfig(
            dashboard_api_base_url="http://127.0.0.1:9000",
            dashboard_display_base_url="http://127.0.0.1:9000/#/backtest",
        )
        self.assertEqual(config.dashboard_api_base_url, "http://127.0.0.1:9000")
        self.assertEqual(
            config.dashboard_display_base_url,
            "http://127.0.0.1:9000/#/backtest",
        )
        self.assertTrue(hasattr(DashboardClient, "from_environment"))

    def test_dashboard_client_authenticates_and_posts_proven_backtest_payload(self) -> None:
        class Response:
            def __init__(self, payload: object) -> None:
                self.body = json.dumps(payload).encode()

            def __enter__(self) -> Response:
                return self

            def __exit__(self, *_args: object) -> None:
                return

            def read(self) -> bytes:
                return self.body

        responses = [
            Response({"auth_token": "secret-token"}),
            Response({"data": {"session": {"state": {"form": {
                "exchange": "Binance Perpetual Futures", "routes": [],
                "start_date": "2024-01-01", "finish_date": "2024-02-01",
            }}}}}),
            Response({"data": {"data": {"backtest": {"balance": 10_000}}}}),
            Response({"status": "started"}),
        ]
        requests = []

        def open_request(request: object, **_kwargs: object) -> Response:
            requests.append(request)
            return responses.pop(0)

        client = DashboardClient.from_environment(
            "http://127.0.0.1:9000", environ={"JESSE_DASHBOARD_PASSWORD": "pw"},
        )
        self.assertIsNotNone(client)
        with patch("urllib.request.urlopen", side_effect=open_request):
            result = client.run_backtest("session-1")
        self.assertEqual(result, {"status": "started"})
        self.assertEqual(requests[-1].full_url, "http://127.0.0.1:9000/backtest")
        payload = json.loads(requests[-1].data)
        self.assertEqual(payload["id"], "session-1")
        self.assertEqual(payload["config"], {"balance": 10_000})
        self.assertEqual(requests[-1].headers["Authorization"], "secret-token")

    def test_restart_resumes_polling_without_create_or_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(tmp, server, max_polls=1)
            first = dispatcher.dispatch(batch_request())
            self.assertEqual(first.payload["results"][0]["outcome"], "retry")
            create_calls = [name for name, _ in server.http.tool_calls if name == "create_backtest_draft"]
            self.assertEqual(len(create_calls), 1)
            second = DirectMcpDispatcher(
                database,
                DirectExecutionConfig(
                    enabled=True, mcp_url=server.url,
                    poll_initial_seconds=0, poll_max_seconds=0, max_polls=3,
                ),
                sleep=lambda _seconds: None,
            ).dispatch(batch_request())
            self.assertEqual(second.payload["results"][0]["outcome"], "finished")
            self.assertEqual(server.http.run_calls, 1)
            self.assertEqual(
                len([name for name, _ in server.http.tool_calls if name == "create_backtest_draft"]),
                1,
            )

    def test_terminal_failure_and_timeout_have_retry_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["stopped"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server)
            failed = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(failed["outcome"], "retry")
            self.assertEqual(failed["blocker_code"], "jesse_execution_stopped")
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["running"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, max_polls=2)
            timed_out = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(timed_out["outcome"], "retry")
            self.assertEqual(timed_out["blocker_code"], "jesse_poll_timeout")

    def test_source_change_gets_one_bounded_preparation_turn(self) -> None:
        fallback = RecordingFallback()
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["finished"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, fallback=fallback)
            result = dispatcher.dispatch(batch_request(change_scope="entry_changed"))
            self.assertEqual(result.payload["results"][0]["outcome"], "finished")
            self.assertEqual(len(fallback.requests), 1)
            self.assertEqual(fallback.requests[0]["task_type"], "prepare_strategies")
            self.assertNotIn("strategy_source", json.dumps(fallback.requests[0]))

    def test_disabled_feature_delegates_to_existing_dispatcher(self) -> None:
        fallback = RecordingFallback()
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            dispatcher = DirectMcpDispatcher(
                database, DirectExecutionConfig(enabled=False), fallback=fallback
            )
            dispatcher.dispatch(batch_request())
            self.assertEqual(fallback.requests, [batch_request()])

    def test_config_loader_enables_direct_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[jesse_executor]\n"
                "enabled = true\n"
                'mcp_url = "http://127.0.0.1:9002/mcp"\n'
                "max_polls = 9\n"
            )
            config = load_direct_execution_config(path)
            self.assertTrue(config.enabled)
            self.assertEqual(config.max_polls, 9)

    def test_optimizer_parameters_use_safe_executor_fallback(self) -> None:
        fallback = RecordingFallback()
        request = batch_request()
        request["requests"][0]["execution_context"] = {
            "optimizer_parameters": {"risk": 0.01},
        }
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            dispatcher = DirectMcpDispatcher(
                database, DirectExecutionConfig(enabled=True), fallback=fallback,
            )
            dispatcher.dispatch(request)
            self.assertEqual(fallback.requests, [request])


if __name__ == "__main__":
    unittest.main()
