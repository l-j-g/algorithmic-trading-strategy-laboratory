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
    McpError,
    classify_jesse_session,
    execution_request_violations,
    execution_result_violations,
    load_direct_execution_config,
)
from ats_lab.models import ExperimentSpec, WorkItem, WorkState
from ats_lab.resources import ResourcePolicy
from ats_lab.session_recovery import SessionRecoveryPolicy
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

SIGNIFICANCE_METRICS = {
    "observed_mean": 0.001234567890123,
    "annualized_return": 12.345678901234,
    "p_value": 0.031234567890123,
    "n_simulations": 5000,
    "n_observations": 1200,
}


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
            elif name == "create_significance_test_draft":
                result = {"significance_test_id": "sig-session-1"}
            elif name in {"run_backtest", "run_significance_test"}:
                self.server.run_calls += 1  # type: ignore[attr-defined]
                result = {"status": "started", "session_id": arguments["session_id"]}
            elif name == "get_backtest_session":
                if arguments["session_id"] in self.server.missing_sessions:  # type: ignore[attr-defined]
                    result = {
                        "data": None,
                        "error": (
                            f"Backtest session {arguments['session_id']} "
                            "not found"
                        ),
                        "message": (
                            f"Backtest session {arguments['session_id']} "
                            "not found"
                        ),
                    }
                    self._reply({
                        "jsonrpc": "2.0", "id": payload.get("id"),
                        "result": result,
                    })
                    return
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
                    session["exception"] = self.server.terminal_exception  # type: ignore[attr-defined]
                result = {"data": {"session": session}, "error": None}
            elif name == "get_significance_test_session":
                poll = self.server.poll_count  # type: ignore[attr-defined]
                self.server.poll_count += 1  # type: ignore[attr-defined]
                statuses = self.server.statuses  # type: ignore[attr-defined]
                status = statuses[min(poll, len(statuses) - 1)]
                # Real Jesse significance sessions expose terminal metrics at
                # the top-level "results" key and store "state" as a config form
                # (no results.executing flag, no metrics key).
                session = {
                    "id": arguments["session_id"],
                    "status": status,
                    "has_results": status == "finished",
                    "results": (
                        self.server.significance_metrics  # type: ignore[attr-defined]
                        if status == "finished" else None
                    ),
                    "state": {"form": {"n_simulations": 5000, "random_seed": 42}},
                    "created_at": 0, "updated_at": 0,
                }
                if status == "stopped":
                    session["exception"] = "significance failure"
                result = {"data": {"session": session}, "error": None}
            else:
                result = {"status": "error", "message": f"unexpected tool {name}"}
        else:
            result = {}
        self._reply({"jsonrpc": "2.0", "id": payload.get("id"), "result": result})

    def do_DELETE(self) -> None:
        self.server.deleted_sessions.append(  # type: ignore[attr-defined]
            self.headers.get("mcp-session-id"),
        )
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

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
        self.http.deleted_sessions = []
        self.http.missing_sessions = set()
        self.http.terminal_exception = "mechanical failure"
        self.http.metrics = {
            "net_profit_percentage": 12.345678901234,
            "route_runs": [
                {"session_id": "route-a", "route": ROUTES[0], "net_profit_percentage": 5.0},
                {"session_id": "route-b", "route": ROUTES[1], "net_profit_percentage": 7.0},
            ],
        }
        self.http.significance_metrics = dict(SIGNIFICANCE_METRICS)
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


class CorrelatedFrameHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        frames = self.server.frames  # type: ignore[attr-defined]
        body = "".join(
            f"event: message\ndata: {frame}\n\n" for frame in frames
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("mcp-session-id", "correlated-session")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CorrelatedMcpServer:
    """Replay a fixed multi-frame SSE stream for JSON-RPC correlation tests."""

    def __init__(self, frames: list[str]) -> None:
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), CorrelatedFrameHandler)
        self.http.frames = frames  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)

    def __enter__(self) -> CorrelatedMcpServer:
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
        self.preparation_outcome = "finished"
        self.preparation_readiness: list[dict] = [{
            "work_item_id": "JOB-1",
            "strategy_name": "ExistingStrategy",
            "status": "ready",
            "contract_checks": [
                {"code": code, "status": "pass"}
                for code in (
                    "positive_quantity", "exit_shape", "indicator_api", "callback_api",
                )
            ],
        }]

    def dispatch(self, request: dict) -> DispatchResult:
        self.requests.append(request)
        if request.get("task_type") == "prepare_strategies":
            ready_ids = [
                entry["work_item_id"]
                for entry in self.preparation_readiness
                if entry.get("status") == "ready"
            ]
            return DispatchResult(
                outcome=self.preparation_outcome,
                payload={
                    "outcome": self.preparation_outcome,
                    "prepared_work_item_ids": ready_ids,
                    "strategy_readiness": self.preparation_readiness,
                },
            )
        return DispatchResult(outcome="finished", payload={"outcome": "finished"})


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


def significance_request(
    *, n_simulations: int = 5000, random_seed: int | None = 42,
) -> dict:
    return {
        "schema_version": 1,
        "task_type": "execute_batch",
        "batch_id": "BATCH-SIG",
        "requests": [{
            "work_item_id": "JOB-SIG",
            "experiment_id": "EXP-SIG",
            "experiment": {
                "strategy_name": "ExistingStrategy",
                "routes": [ROUTES[0]],
                "sizing_model": "risk quantity capped at 95% available_margin",
            },
            "work_item": {
                "operation": "significance",
                "parameters": {
                    "n_simulations": n_simulations,
                    "random_seed": random_seed,
                },
            },
        }],
    }


class DirectMcpExecutorTests(unittest.TestCase):
    def test_session_classifier_distinguishes_active_zombie_draft_failure_and_success(self) -> None:
        active = {
            "status": "running", "updated_at": 1_000,
            "state": {"results": {"executing": True, "progressbar": {"current": 2}}},
        }
        zombie = {
            "status": "running", "updated_at": 1_000,
            "execution_duration": None,
            "state": {"results": {
                "executing": False, "progressbar": {"current": 0},
                "metrics": {}, "trades": [], "charts": {"equity_curve": []},
                "exception": {"error": None, "traceback": None},
            }},
        }
        draft = {"status": "draft", "state": {"results": {}}}
        stopped = {
            "status": "stopped", "execution_duration": 3.2,
            "state": {"results": {
                "executing": False,
                "exception": {"error": "mechanical failure", "traceback": None},
            }},
        }
        finished = {
            "status": "finished", "metrics": {"total": 10},
            "state": {"results": {"executing": False}},
        }

        self.assertEqual(classify_jesse_session(active).state, "active_execution")
        self.assertEqual(
            classify_jesse_session(zombie, unchanged_observations=1,
                                   stale_for_seconds=300, grace_seconds=60).state,
            "temporarily_nonterminal",
        )
        self.assertEqual(
            classify_jesse_session(zombie, unchanged_observations=2,
                                   stale_for_seconds=300, grace_seconds=60).state,
            "zombie_nonexecuting",
        )
        self.assertEqual(classify_jesse_session(draft).state, "draft_not_started")
        self.assertEqual(classify_jesse_session(stopped).state, "terminal_failure")
        self.assertEqual(classify_jesse_session(finished).state, "terminal_success")

    def test_malformed_session_fails_closed(self) -> None:
        self.assertEqual(
            classify_jesse_session({"status": "running", "state": {}}).state,
            "malformed_session",
        )

    def test_session_recovery_policy_is_bounded(self) -> None:
        policy = SessionRecoveryPolicy()
        self.assertFalse(policy.exhausted("malformed_session"))
        self.assertTrue(policy.exhausted(
            "malformed_session", recovery_attempted=True,
        ))
        self.assertTrue(policy.exhausted("start_recovery_failed"))
        self.assertTrue(policy.exhausted("zombie_recovery_required"))

    def make_dispatcher(
        self,
        root: str,
        server: FakeMcpServer,
        *,
        max_polls: int = 4,
        fallback: RecordingFallback | None = None,
        dashboard: FakeDashboard | None = None,
        work_id: str = "JOB-1",
        experiment_id: str = "EXP-1",
        specification: dict | None = None,
        poll_initial_seconds: float = 0,
        resource_policy: ResourcePolicy | None = None,
    ) -> tuple[DirectMcpDispatcher, WorkflowDatabase]:
        database = WorkflowDatabase(Path(root) / "lab.sqlite3")
        database.initialize()
        database.upsert_experiment(ExperimentSpec(
            id=experiment_id, strategy_name="ExistingStrategy",
        ))
        database.upsert_work_item(WorkItem(
            id=work_id, experiment_id=experiment_id, priority=1,
            state=WorkState.RUNNING,
            specification=specification or {"operation": "backtest"},
        ))
        dispatcher = DirectMcpDispatcher(
            database,
            DirectExecutionConfig(
                enabled=True,
                mcp_url=server.url,
                poll_initial_seconds=poll_initial_seconds,
                poll_max_seconds=0,
                max_polls=max_polls,
            ),
            fallback=fallback,
            sleep=lambda _seconds: None,
            dashboard_client=dashboard,
            resource_policy=resource_policy,
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

    def test_client_selects_response_matching_request_id(self) -> None:
        frames = [
            json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 99, "result": {"foreign": True}}),
            json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"value": 42}}),
        ]
        with CorrelatedMcpServer(frames) as server:
            client = McpClient(server.url)
            client.next_id = 7
            result = client.post({"jsonrpc": "2.0", "id": 7, "method": "x"})
        self.assertEqual(result, {"jsonrpc": "2.0", "id": 7, "result": {"value": 42}})

    def test_client_discards_interleaved_frames_before_match(self) -> None:
        frames = [
            json.dumps({"jsonrpc": "2.0", "method": "notify", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 3, "result": {"stale": 1}}),
            json.dumps({"jsonrpc": "2.0", "id": 4, "result": {"fresh": 1}}),
        ]
        with CorrelatedMcpServer(frames) as server:
            client = McpClient(server.url)
            client.next_id = 4
            result = client.post({"jsonrpc": "2.0", "id": 4, "method": "x"})
        self.assertEqual(result["result"], {"fresh": 1})

    def test_client_raises_when_no_frame_matches_request_id(self) -> None:
        frames = [
            json.dumps({"jsonrpc": "2.0", "id": 11, "result": {}}),
            json.dumps({"jsonrpc": "2.0", "method": "notify", "params": {}}),
        ]
        with CorrelatedMcpServer(frames) as server:
            client = McpClient(server.url)
            client.next_id = 12
            with self.assertRaises(McpError) as caught:
                client.post({"jsonrpc": "2.0", "id": 12, "method": "x"})
        self.assertIn("id mismatch", str(caught.exception))
        self.assertIn("11", str(caught.exception))

    def test_notification_post_without_id_tolerates_empty_body(self) -> None:
        with CorrelatedMcpServer([]) as server:
            client = McpClient(server.url)
            self.assertIsNone(client.post({
                "jsonrpc": "2.0", "method": "notifications/initialized",
                "params": {},
            }))

    def test_dispatch_sends_session_delete_teardown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server)
            result = dispatcher.dispatch(batch_request())
            self.assertEqual(result.outcome, "finished")
            self.assertEqual(
                server.http.deleted_sessions, ["mcp-test-session"],
            )

    def test_dispatch_runs_independent_direct_items_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer([]) as server:
            dispatcher, _ = self.make_dispatcher(
                tmp, server,
                resource_policy=ResourcePolicy(execution_parallelism=2),
            )
            first = batch_request()["requests"][0]
            second = json.loads(json.dumps(first))
            second["work_item_id"] = "JOB-2"
            second["experiment_id"] = "EXP-2"
            barrier = threading.Barrier(2)
            active = 0
            peak = 0
            lock = threading.Lock()

            def execute(item: dict) -> dict:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    try:
                        barrier.wait(timeout=2)
                    except threading.BrokenBarrierError:
                        pass
                    return {
                        "work_item_id": item["work_item_id"],
                        "outcome": "retry",
                        "blocker_code": "test_parallelism",
                        "detail": "test result",
                    }
                finally:
                    with lock:
                        active -= 1

            with patch.object(dispatcher, "_execute_one", side_effect=execute):
                result = dispatcher.dispatch({
                    "task_type": "execute_batch",
                    "requests": [first, second],
                })

            self.assertEqual(peak, 2)
            self.assertEqual(
                [item["work_item_id"] for item in result.payload["results"]],
                ["JOB-1", "JOB-2"],
            )

    def test_deferred_dispatch_still_tears_down_session_at_final_poll(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running"]
        ) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, max_polls=1)
            result = dispatcher.dispatch(batch_request())
            self.assertEqual(result.payload["results"][0]["outcome"], "retry")
            self.assertEqual(
                server.http.deleted_sessions, ["mcp-test-session"],
            )

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

    def test_execution_request_validator_accepts_canonical_item(self) -> None:
        self.assertEqual(execution_request_violations(
            batch_request()["requests"][0],
        ), [])
        self.assertEqual(execution_request_violations(
            significance_request()["requests"][0],
        ), [])

    def test_significance_validator_requires_one_primary_route(self) -> None:
        item = significance_request()["requests"][0]
        item["experiment"]["routes"] = ROUTES
        violations = execution_request_violations(item)
        self.assertIn(
            "significance requires exactly one primary trading route; "
            "run one test per symbol/timeframe",
            violations,
        )

    def test_multi_route_significance_is_blocked_before_mcp(self) -> None:
        request = significance_request()
        request["requests"][0]["experiment"]["routes"] = ROUTES
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, _database = self.make_dispatcher(
                tmp, server, work_id="JOB-SIG", experiment_id="EXP-SIG",
                specification={
                    "operation": "significance",
                    "parameters": {"n_simulations": 5000},
                },
            )
            result = dispatcher.dispatch(request)
            item = result.payload["results"][0]
            self.assertEqual(item["outcome"], "blocked")
            self.assertEqual(item["blocker_code"], "strategy_contract_invalid")
            self.assertIn("exactly one primary trading route", item["detail"])
            self.assertEqual(server.http.tool_calls, [])

    def test_execution_request_validator_reports_schema_violations(self) -> None:
        item = batch_request()["requests"][0]
        item["schema_version"] = 2
        item["work_item_id"] = ""
        del item["experiment_id"]
        item["experiment"]["strategy_name"] = 7
        item["experiment"]["routes"] = [{
            "exchange": "Binance Perpetual Futures", "symbol": "BTC-USDT",
            "timeframe": "1h", "start_date": "2024-01-01",
        }]
        item["work_item"]["operation"] = "voodoo"
        item["work_item"]["parameters"] = "n=5"
        violations = execution_request_violations(item)
        self.assertIn("schema_version must be 1", violations)
        self.assertIn("work_item_id must be non-empty text", violations)
        self.assertIn("experiment_id must be non-empty text", violations)
        self.assertIn("experiment.strategy_name must be non-empty text", violations)
        self.assertIn(
            "experiment.routes[0].finish_date must be non-empty text", violations,
        )
        self.assertIn(
            "work_item.operation must be one of backtest, hpo, monte_carlo, "
            "significance",
            violations,
        )
        self.assertIn("work_item.parameters must be an object", violations)

    def test_execution_request_validator_checks_gate_shapes(self) -> None:
        item = batch_request()["requests"][0]
        item["experiment"]["success_gates"] = [
            {"name": "sharpe", "operator": ">=", "threshold": 0.0, "required": True},
            {"name": "", "operator": ">="},
            {"operator": ">="},
            {"name": "x", "operator": ">=", "required": "yes"},
            "sharpe >= 0",
        ]
        item["experiment"]["failure_gates"] = {"name": "dd"}
        violations = execution_request_violations(item)
        self.assertIn("experiment.success_gates[1].name must be non-empty text", violations)
        self.assertIn("experiment.success_gates[2].name must be non-empty text", violations)
        self.assertIn(
            "experiment.success_gates[3].required must be boolean", violations,
        )
        self.assertIn("experiment.success_gates[4] must be an object", violations)
        self.assertIn("experiment.failure_gates must be an array", violations)

    def test_dispatch_blocks_schema_invalid_request_without_mcp_traffic(self) -> None:
        request = batch_request()
        route = dict(request["requests"][0]["experiment"]["routes"][0])
        del route["finish_date"]
        request["requests"][0]["experiment"]["routes"] = [route]
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server)
            result = dispatcher.dispatch(request)
        item_result = result.payload["results"][0]
        self.assertEqual(item_result["outcome"], "blocked")
        self.assertEqual(item_result["blocker_code"], "strategy_contract_invalid")
        self.assertIn(
            "jesse-execution-request schema violation", item_result["detail"],
        )
        self.assertEqual(server.http.requests, [])

    def test_execution_result_validator_enforces_exact_raw_result_keys(self) -> None:
        view = {
            "schema_version": 1, "transport": "jesse_mcp",
            "work_item_id": "JOB-1", "status": "finished",
            "session_id": "s-1", "dashboard_url": None,
            "metrics": {"total": 10}, "error": None,
            "raw_result": {
                "session_id": "s-1", "status": "finished",
                "metrics": {"total": 10},
            },
        }
        self.assertEqual(execution_result_violations(view), [])
        view["raw_result"]["extra"] = True
        self.assertIn(
            "raw_result must contain exactly session_id, status, and metrics",
            execution_result_violations(view),
        )
        view["raw_result"] = {
            "session_id": "other", "status": "finished", "metrics": {},
        }
        violations = execution_result_violations(view)
        self.assertIn("raw_result.session_id must equal session_id", violations)
        self.assertIn("raw_result.metrics must equal metrics", violations)
        view["raw_result"] = {
            "session_id": "s-1", "status": "stopped", "metrics": {"total": 10},
        }
        self.assertIn(
            "raw_result.status must equal status",
            execution_result_violations(view),
        )

    def test_execution_result_validator_reports_envelope_violations(self) -> None:
        violations = execution_result_violations({
            "schema_version": 2, "transport": "agent",
            "work_item_id": "", "status": "wrapped",
            "session_id": None, "dashboard_url": 5,
            "metrics": [], "error": {"code": "x"},
            "raw_result": {},
        })
        self.assertIn("schema_version must be 1", violations)
        self.assertIn("transport must be jesse_mcp", violations)
        self.assertIn("work_item_id must be non-empty text", violations)
        self.assertIn(
            "status must be one of draft, finished, running, stopped, "
            "terminated, unknown",
            violations,
        )
        self.assertIn("session_id must be non-empty text", violations)
        self.assertIn("dashboard_url must be text or null", violations)
        self.assertIn("metrics must be an object", violations)
        self.assertIn("finished result cannot contain an error", violations)
        self.assertEqual(execution_result_violations("nope"), [
            "execution result must be an object",
        ])

    def test_finished_persist_boundary_blocks_invalid_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server, patch(
            "ats_lab.direct_mcp_executor.execution_result_violations",
            return_value=["metrics must be an object"],
        ):
            dispatcher, database = self.make_dispatcher(tmp, server)
            result = dispatcher.dispatch(batch_request())
            item_result = result.payload["results"][0]
            self.assertEqual(item_result["outcome"], "blocked")
            self.assertEqual(item_result["blocker_code"], "invalid_execution_result")
            self.assertIn(
                "jesse-execution-result schema violation", item_result["detail"],
            )
            telemetry = database.rows(
                "SELECT outcome FROM direct_execution_telemetry "
                "ORDER BY id DESC LIMIT 1"
            )[0]
            self.assertEqual(telemetry["outcome"], "blocked")

    def test_uncharged_infrastructure_failures_open_recoverable_circuit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running"]
        ) as server:
            dispatcher, database = self.make_dispatcher(
                tmp, server, max_polls=1,
                resource_policy=ResourcePolicy(
                    executor_infrastructure_failure_limit=2,
                ),
            )
            first = dispatcher.dispatch(batch_request())
            self.assertEqual(first.payload["results"][0]["outcome"], "retry")
            second = dispatcher.dispatch(batch_request())
            blocked_item = second.payload["results"][0]
            self.assertEqual(blocked_item["outcome"], "blocked")
            self.assertEqual(
                blocked_item["blocker_code"], "infrastructure_circuit_broken",
            )
            self.assertIn(
                "consecutive uncharged infrastructure", blocked_item["detail"],
            )
            self.assertTrue(blocked_item["attempt_charged"])
            outcomes = [
                row["outcome"] for row in database.rows(
                    """SELECT outcome FROM direct_execution_telemetry
                       WHERE work_item_id='JOB-1' ORDER BY id""",
                )
            ]
            self.assertEqual(outcomes, ["retry", "blocked"])
            server.http.statuses = ["running", "finished"]
            recovered = dispatcher.dispatch(batch_request())
            self.assertEqual(recovered.payload["results"][0]["outcome"], "finished")
            outcomes = [
                row["outcome"] for row in database.rows(
                    """SELECT outcome FROM direct_execution_telemetry
                       WHERE work_item_id='JOB-1' ORDER BY id""",
                )
            ]
            self.assertEqual(outcomes, ["retry", "blocked", "finished"])

    def test_failed_replacement_draft_creation_releases_reservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(tmp, server)
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO direct_execution_recoveries(
                           work_item_id,old_session_id,old_state,reason,
                           replacement_allowed,replacement_reserved,
                           created_at,updated_at
                       ) VALUES (
                           'JOB-1','old-session','zombie_nonexecuting',
                           'zombie recovery',1,0,?,?)""",
                    ("2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"),
                )
            with patch.object(
                dispatcher, "_create",
                side_effect=McpError("draft create failed"),
            ):
                failed = dispatcher.dispatch(batch_request())
            self.assertEqual(
                failed.payload["results"][0]["blocker_code"], "direct_mcp_error",
            )
            row = database.rows(
                """SELECT replacement_reserved,replacement_session_id
                   FROM direct_execution_recoveries WHERE work_item_id='JOB-1'""",
            )[0]
            self.assertEqual(row["replacement_reserved"], 0)
            self.assertIsNone(row["replacement_session_id"])
            recovered = dispatcher.dispatch(batch_request())
            self.assertEqual(
                recovered.payload["results"][0]["outcome"], "finished",
            )
            row = database.rows(
                """SELECT replacement_reserved,replacement_session_id
                   FROM direct_execution_recoveries WHERE work_item_id='JOB-1'""",
            )[0]
            self.assertEqual(row["replacement_reserved"], 1)
            self.assertEqual(row["replacement_session_id"], "jesse-session-1")

    def test_terminal_metrics_sanitize_non_finite_floats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(tmp, server)
            server.http.metrics = {
                "net_profit_percentage": float("nan"),
                "sharpe_ratio": float("inf"),
                "total": 10,
                "route_runs": [{"session_id": "route-a", "fee": float("-inf")}],
            }
            result = dispatcher.dispatch(batch_request())
            run = result.payload["results"][0]["evidence"]["run"]
            self.assertIsNone(run["metrics"]["net_profit_percentage"])
            self.assertIsNone(run["metrics"]["sharpe_ratio"])
            self.assertEqual(run["metrics"]["total"], 10)
            self.assertIsNone(run["metrics"]["route_runs"][0]["fee"])
            checkpoint = database.rows(
                "SELECT metrics_json FROM direct_execution_sessions "
                "WHERE work_item_id='JOB-1'"
            )[0]
            self.assertNotIn("NaN", checkpoint["metrics_json"])
            self.assertNotIn("Infinity", checkpoint["metrics_json"])
            self.assertIn('"net_profit_percentage":null', checkpoint["metrics_json"])

    def test_checkpoint_save_failure_records_orphaned_draft_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(tmp, server)
            with patch.object(
                dispatcher, "_save_checkpoint",
                side_effect=OSError("database is locked"),
            ):
                result = dispatcher.dispatch(batch_request())
            item_result = result.payload["results"][0]
            self.assertEqual(item_result["blocker_code"], "direct_mcp_error")
            events = database.rows(
                """SELECT payload_json FROM events WHERE aggregate_id='JOB-1'
                   AND event_type='direct_execution_draft_orphaned'""",
            )
            self.assertEqual(len(events), 1)
            self.assertEqual(
                json.loads(events[0]["payload_json"]),
                {"experiment_id": "EXP-1", "session_id": "jesse-session-1"},
            )

    def test_backtest_forwards_work_item_data_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, _database = self.make_dispatcher(tmp, server)
            request = batch_request()
            request["requests"][0]["work_item"]["data_routes"] = [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "BTC-USDT",
                "timeframe": "4h",
            }]
            result = dispatcher.dispatch(request)
            self.assertEqual(result.outcome, "finished")
            create_args = next(
                args for name, args in server.http.tool_calls
                if name == "create_backtest_draft"
            )
            self.assertEqual(
                json.loads(create_args["data_routes"]),
                request["requests"][0]["work_item"]["data_routes"],
            )

    def test_trusted_strategy_dependency_is_sent_as_data_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["finished"]
        ) as server:
            dispatcher, _database = self.make_dispatcher(tmp, server)
            request = batch_request()
            request["requests"][0]["experiment"]["strategy_name"] = (
                "EthBtcRatioZscoreRevert"
            )
            request["requests"][0]["experiment"]["routes"] = [ROUTES[1]]
            result = dispatcher.dispatch(request)
            self.assertEqual(result.payload["results"][0]["outcome"], "finished")
            create_args = next(
                args for name, args in server.http.tool_calls
                if name == "create_backtest_draft"
            )
            self.assertEqual(json.loads(create_args["data_routes"]), [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "BTC-USDT",
                "timeframe": "1h",
            }])

    def test_experiment_and_work_item_data_routes_are_unioned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["finished"]
        ) as server:
            dispatcher, _database = self.make_dispatcher(tmp, server)
            request = batch_request()
            item = request["requests"][0]
            item["experiment"]["data_routes"] = [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "BTC-USDT",
                "timeframe": "4h",
            }]
            item["work_item"]["data_routes"] = [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "ETH-USDT",
                "timeframe": "1h",
            }]
            result = dispatcher.dispatch(request)
            self.assertEqual(result.payload["results"][0]["outcome"], "finished")
            create_args = next(
                args for name, args in server.http.tool_calls
                if name == "create_backtest_draft"
            )
            self.assertEqual(json.loads(create_args["data_routes"]), [
                item["experiment"]["data_routes"][0],
                item["work_item"]["data_routes"][0],
            ])

    def test_missing_persisted_session_gets_one_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(
                tmp, server, max_polls=2,
            )
            request = batch_request()["requests"][0]
            fingerprint = dispatcher._fingerprint(request)
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO direct_execution_sessions(
                           work_item_id,experiment_id,session_id,
                           request_fingerprint,state,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        "JOB-1", "EXP-1", "old-session", fingerprint,
                        "start_recovery_failed", "now", "now",
                    ),
                )
            server.http.missing_sessions.add("old-session")

            first = dispatcher.dispatch(batch_request()).payload["results"][0]

            self.assertEqual(first["outcome"], "retry")
            self.assertEqual(first["blocker_code"], "jesse_session_recovery_pending")
            self.assertFalse(first["attempt_charged"])
            self.assertEqual(
                database.rows(
                    "SELECT COUNT(*) AS count FROM direct_execution_sessions "
                    "WHERE work_item_id='JOB-1'"
                )[0]["count"],
                0,
            )
            recovery = database.rows(
                """SELECT old_session_id,replacement_allowed,
                          replacement_reserved,replacement_session_id
                   FROM direct_execution_recoveries WHERE work_item_id='JOB-1'"""
            )[0]
            self.assertEqual(recovery["old_session_id"], "old-session")
            self.assertEqual(recovery["replacement_allowed"], 1)
            self.assertEqual(recovery["replacement_reserved"], 0)
            self.assertIsNone(recovery["replacement_session_id"])
            self.assertEqual(
                database.rows(
                    """SELECT event_type FROM events WHERE aggregate_id='JOB-1'
                       ORDER BY id DESC LIMIT 1"""
                )[0]["event_type"],
                "missing_execution_session_recovered",
            )

            second = dispatcher.dispatch(batch_request()).payload["results"][0]

            self.assertEqual(second["outcome"], "finished")
            self.assertEqual(
                len([
                    name for name, _ in server.http.tool_calls
                    if name == "create_backtest_draft"
                ]),
                1,
            )
            recovery = database.rows(
                """SELECT replacement_reserved,replacement_session_id
                   FROM direct_execution_recoveries WHERE work_item_id='JOB-1'"""
            )[0]
            self.assertEqual(recovery["replacement_reserved"], 1)
            self.assertEqual(recovery["replacement_session_id"], "jesse-session-1")

    def test_missing_replacement_session_does_not_get_second_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(tmp, server)
            request = batch_request()["requests"][0]
            fingerprint = dispatcher._fingerprint(request)
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO direct_execution_recoveries(
                           work_item_id,old_session_id,old_state,reason,
                           replacement_allowed,replacement_reserved,
                           replacement_session_id,created_at,updated_at
                       ) VALUES (?,?,?,?,1,1,?,?,?)""",
                    (
                        "JOB-1", "old-session", "start_recovery_failed",
                        "previous missing session recovery", "replacement-session",
                        "now", "now",
                    ),
                )
                connection.execute(
                    """INSERT INTO direct_execution_sessions(
                           work_item_id,experiment_id,session_id,
                           request_fingerprint,state,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        "JOB-1", "EXP-1", "replacement-session", fingerprint,
                        "start_recovery_failed", "now", "now",
                    ),
                )
            server.http.missing_sessions.add("replacement-session")

            result = dispatcher.dispatch(batch_request()).payload["results"][0]

            self.assertEqual(result["outcome"], "blocked")
            self.assertEqual(
                result["blocker_code"], "jesse_session_recovery_exhausted",
            )
            self.assertEqual(
                len([
                    name for name, _ in server.http.tool_calls
                    if name == "create_backtest_draft"
                ]),
                0,
            )

    def test_backtest_draft_snapshots_exchange_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, _database = self.make_dispatcher(tmp, server)
            request = batch_request()
            request["requests"][0]["experiment"].update({
                "balance": 1_000,
                "fee_rate": 0.001,
                "leverage": 3,
                "leverage_mode": "isolated",
            })
            result = dispatcher.dispatch(request)
            self.assertEqual(result.outcome, "finished")
            create_args = next(
                args for name, args in server.http.tool_calls
                if name == "create_backtest_draft"
            )
            self.assertEqual({
                key: create_args[key] for key in (
                    "balance", "fee", "futures_leverage", "futures_leverage_mode",
                )
            }, {
                "balance": 1_000,
                "fee": 0.001,
                "futures_leverage": 3,
                "futures_leverage_mode": "isolated",
            })

    def test_beta_without_btc_benchmark_data_route_blocks_before_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, _database = self.make_dispatcher(tmp, server)
            request = batch_request()
            request["requests"][0]["experiment"]["variant"] = "btc_beta"
            result = dispatcher.dispatch(request)

            item = result.payload["results"][0]
            self.assertEqual(item["outcome"], "blocked")
            self.assertEqual(item["blocker_code"], "strategy_contract_invalid")
            self.assertIn("missing_beta_benchmark_data_route", item["detail"])
            self.assertEqual(server.http.tool_calls, [])

    def test_significance_dispatch_direct_zero_model_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(
                tmp, server, work_id="JOB-SIG", experiment_id="EXP-SIG",
                specification={
                    "operation": "significance",
                    "parameters": {"n_simulations": 5000, "random_seed": 42},
                },
            )
            result = dispatcher.dispatch(significance_request())
            self.assertEqual(result.outcome, "finished")
            run = result.payload["results"][0]["evidence"]["run"]
            self.assertEqual(run["metrics"], server.http.significance_metrics)
            self.assertEqual(run["status"], "finished")
            self.assertEqual(
                run["raw_result"],
                {
                    "session_id": "sig-session-1",
                    "status": "finished",
                    "metrics": server.http.significance_metrics,
                },
            )
            tools = [name for name, _ in server.http.tool_calls]
            self.assertIn("create_significance_test_draft", tools)
            self.assertIn("run_significance_test", tools)
            self.assertNotIn("run_backtest", tools)
            sig_args = next(
                args for name, args in server.http.tool_calls
                if name == "create_significance_test_draft"
            )
            self.assertEqual({
                key: sig_args[key] for key in (
                    "balance", "fee", "futures_leverage", "futures_leverage_mode",
                )
            }, {
                "balance": 10_000,
                "fee": 0.0005,
                "futures_leverage": 1,
                "futures_leverage_mode": "cross",
            })
            self.assertEqual(server.http.run_calls, 1)
            telemetry = database.rows(
                "SELECT * FROM direct_execution_telemetry ORDER BY id DESC LIMIT 1"
            )[0]
            self.assertEqual(telemetry["model_call_count"], 0)

    def test_significance_forwards_work_item_data_routes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, _database = self.make_dispatcher(
                tmp, server, work_id="JOB-SIG", experiment_id="EXP-SIG",
                specification={
                    "operation": "significance",
                    "parameters": {"n_simulations": 5000},
                },
            )
            request = significance_request()
            request["requests"][0]["work_item"]["data_routes"] = [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "BTC-USDT",
                "timeframe": "4h",
            }]

            result = dispatcher.dispatch(request)

            self.assertEqual(result.outcome, "finished")
            create_args = next(
                args for name, args in server.http.tool_calls
                if name == "create_significance_test_draft"
            )
            self.assertEqual(
                json.loads(create_args["data_routes"]),
                request["requests"][0]["work_item"]["data_routes"],
            )

    def test_significance_draft_defaults_explicit_null_exchange_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "finished"]
        ) as server:
            dispatcher, _database = self.make_dispatcher(
                tmp, server, work_id="JOB-SIG", experiment_id="EXP-SIG",
                specification={
                    "operation": "significance",
                    "parameters": {"n_simulations": 5000},
                },
            )
            request = significance_request()
            request["requests"][0]["experiment"].update({
                "balance": None,
                "starting_balance": None,
                "fee_rate": None,
                "fee": None,
                "futures_leverage": None,
                "leverage": None,
                "futures_leverage_mode": None,
                "leverage_mode": None,
            })

            result = dispatcher.dispatch(request)

            self.assertEqual(result.outcome, "finished")
            sig_args = next(
                args for name, args in server.http.tool_calls
                if name == "create_significance_test_draft"
            )
            self.assertEqual({
                key: sig_args[key] for key in (
                    "balance", "fee", "futures_leverage", "futures_leverage_mode",
                )
            }, {
                "balance": 10_000,
                "fee": 0.0005,
                "futures_leverage": 1,
                "futures_leverage_mode": "cross",
            })

    def test_significance_incomplete_metrics_retries_without_attempt_charge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["finished"]
        ) as server:
            server.http.significance_metrics = {
                "observed_mean": 0.001,
                "annualized_return": 12.3,
            }
            dispatcher, _ = self.make_dispatcher(
                tmp, server, work_id="JOB-SIG", experiment_id="EXP-SIG",
                specification={
                    "operation": "significance",
                    "parameters": {"n_simulations": 5000},
                },
            )
            result = dispatcher.dispatch(significance_request())
            item = result.payload["results"][0]
            self.assertEqual(item["outcome"], "retry")
            self.assertEqual(item["blocker_code"], "invalid_jesse_metrics")

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

    def test_asynchronous_start_landing_prevents_double_dashboard_start(self) -> None:
        dashboard = FakeDashboard()
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["draft", "running", "finished"]
        ) as server:
            dispatcher, _ = self.make_dispatcher(
                tmp, server, dashboard=dashboard, poll_initial_seconds=0.01,
            )
            result = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(result["outcome"], "finished")
            self.assertEqual(dashboard.started, [])
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
            terminal = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(terminal["outcome"], "blocked")
            self.assertTrue(terminal["attempt_charged"])
            self.assertIn("bounded session recovery exhausted", terminal["detail"])

    def test_malformed_session_reconciles_once_then_routes_to_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["running"]) as server:
            dispatcher, database = self.make_dispatcher(tmp, server)
            malformed = {
                "id": "jesse-session-1", "status": "running", "state": {},
            }
            with patch.object(dispatcher, "_fetch_session", return_value=malformed):
                first = dispatcher.dispatch(batch_request()).payload["results"][0]
                second = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(first["outcome"], "retry")
            self.assertFalse(first["attempt_charged"])
            self.assertEqual(second["outcome"], "blocked")
            self.assertTrue(second["attempt_charged"])
            self.assertIn("bounded reconciliation", second["detail"])
            checkpoint = database.rows(
                "SELECT state,recovery_attempted FROM direct_execution_sessions "
                "WHERE work_item_id='JOB-1'"
            )[0]
            self.assertEqual(checkpoint["state"], "malformed_session")
            self.assertEqual(checkpoint["recovery_attempted"], 1)

    def test_zombie_session_is_terminal_after_one_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["running"]) as server:
            dispatcher, _database = self.make_dispatcher(tmp, server, max_polls=2)
            zombie = {
                "id": "jesse-session-1", "status": "running",
                "updated_at": "1970-01-01T00:00:00Z",
                "state": {"results": {
                    "executing": False, "progressbar": {"current": 0},
                    "metrics": {}, "trades": [], "charts": {"equity_curve": []},
                }},
            }
            with patch.object(dispatcher, "_fetch_session", return_value=zombie):
                first = dispatcher.dispatch(batch_request()).payload["results"][0]
                second = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(first["blocker_code"], "jesse_zombie_recovery_pending")
            self.assertEqual(first["outcome"], "retry")
            self.assertEqual(second["blocker_code"], "jesse_zombie_recovery_required")
            self.assertEqual(second["outcome"], "blocked")
            self.assertTrue(second["attempt_charged"])

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

            def close(self) -> None:
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
                "balance": 1_000,
                "fee": 0.001,
                "futures_leverage": 3,
                "futures_leverage_mode": "isolated",
                "config": {"balance": 5_000},
            }}}}}),
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
        self.assertEqual(payload["config"], {"balance": 5_000})
        self.assertEqual({
            key: payload[key] for key in (
                "balance", "fee", "futures_leverage", "futures_leverage_mode",
            )
        }, {
            "balance": 1_000,
            "fee": 0.001,
            "futures_leverage": 3,
            "futures_leverage_mode": "isolated",
        })
        self.assertEqual(requests[-1].headers["Authorization"], "secret-token")

    def test_fingerprint_changes_when_session_exchange_config_changes(self) -> None:
        request = batch_request()["requests"][0]
        baseline = DirectMcpDispatcher._fingerprint(request)

        for field, value in (
            ("balance", 1_000),
            ("fee_rate", 0.001),
            ("leverage", 3),
            ("leverage_mode", "isolated"),
        ):
            with self.subTest(field=field):
                mutated = json.loads(json.dumps(request))
                mutated["experiment"][field] = value
                self.assertNotEqual(
                    DirectMcpDispatcher._fingerprint(mutated), baseline,
                )

        mutated = json.loads(json.dumps(request))
        mutated["work_item"]["data_routes"] = [{
            "exchange": "Binance Perpetual Futures",
            "symbol": "BTC-USDT",
            "timeframe": "4h",
        }]
        self.assertNotEqual(
            DirectMcpDispatcher._fingerprint(mutated), baseline,
        )

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

    def test_terminal_failure_is_analyzable_while_timeout_retries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["stopped"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server)
            failed = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(failed["outcome"], "blocked")
            self.assertEqual(failed["blocker_code"], "jesse_execution_stopped")
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["running"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, max_polls=2)
            timed_out = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(timed_out["outcome"], "retry")
            self.assertEqual(timed_out["blocker_code"], "jesse_execution_deferred")

    def test_missing_data_route_is_operator_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["stopped"]) as server:
            server.http.terminal_exception = (
                "RouteNotFound: Data route is required but missing: "
                "symbol='BTC-USDT', timeframe='1m'"
            )
            dispatcher, _database = self.make_dispatcher(tmp, server)
            result = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(result["outcome"], "blocked")
            self.assertEqual(result["blocker_code"], "missing_data_route")

    def test_active_poll_slice_is_deferred_without_strategy_attempt_charge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["running"]) as server:
            dispatcher, database = self.make_dispatcher(tmp, server, max_polls=1)
            result = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(result["outcome"], "retry")
            self.assertEqual(result["blocker_code"], "jesse_execution_deferred")
            self.assertFalse(result["attempt_charged"])
            checkpoint = database.rows(
                "SELECT state,unchanged_observations FROM direct_execution_sessions "
                "WHERE work_item_id='JOB-1'"
            )[0]
            self.assertEqual(checkpoint["state"], "active_execution")
            self.assertGreaterEqual(checkpoint["unchanged_observations"], 1)

    def test_recovered_zombie_creates_exactly_one_replacement_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(
            ["running", "running", "finished"]
        ) as server:
            dispatcher, database = self.make_dispatcher(tmp, server, max_polls=1)
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO direct_execution_recoveries(
                           work_item_id,old_session_id,old_state,reason,
                           replacement_allowed,created_at,updated_at
                       ) VALUES (?,?,?,?,1,?,?)""",
                    (
                        "JOB-1", "old-zombie", "zombie_nonexecuting",
                        "no execution evidence", "now", "now",
                    ),
                )
            first = dispatcher.dispatch(batch_request()).payload["results"][0]
            self.assertEqual(first["blocker_code"], "jesse_execution_deferred")
            second = DirectMcpDispatcher(
                database,
                DirectExecutionConfig(
                    enabled=True, mcp_url=server.url, max_polls=2,
                    poll_initial_seconds=0, poll_max_seconds=0,
                ),
                sleep=lambda _seconds: None,
            ).dispatch(batch_request()).payload["results"][0]
            self.assertEqual(second["outcome"], "finished")
            self.assertEqual(
                len([name for name, _ in server.http.tool_calls
                     if name == "create_backtest_draft"]),
                1,
            )
            recovery = database.rows(
                "SELECT replacement_reserved,replacement_session_id "
                "FROM direct_execution_recoveries WHERE work_item_id='JOB-1'"
            )[0]
            self.assertEqual(recovery["replacement_reserved"], 1)
            self.assertEqual(recovery["replacement_session_id"], "jesse-session-1")
            self.assertEqual(database.rows(
                "SELECT replacement_created FROM direct_execution_sessions "
                "WHERE work_item_id='JOB-1'"
            )[0]["replacement_created"], 1)

    def test_existing_replacement_checkpoint_is_adopted_without_new_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["finished"]) as server:
            dispatcher, database = self.make_dispatcher(tmp, server)
            fingerprint = dispatcher._fingerprint(batch_request()["requests"][0])
            with database.connect() as connection:
                connection.execute(
                    """INSERT INTO direct_execution_recoveries(
                           work_item_id,old_session_id,old_state,reason,
                           replacement_allowed,created_at,updated_at
                       ) VALUES (?,?,?,?,1,?,?)""",
                    (
                        "JOB-1", "old-zombie", "zombie_nonexecuting",
                        "no execution evidence", "now", "now",
                    ),
                )
                connection.execute(
                    """INSERT INTO direct_execution_sessions(
                           work_item_id,experiment_id,session_id,request_fingerprint,
                           state,created_at,updated_at
                       ) VALUES (?,?,?,?,?,?,?)""",
                    (
                        "JOB-1", "EXP-1", "jesse-session-1", fingerprint,
                        "start_recovery_failed", "now", "now",
                    ),
                )

            result = dispatcher.dispatch(batch_request()).payload["results"][0]

            self.assertEqual(result["outcome"], "finished")
            self.assertEqual(
                len([name for name, _ in server.http.tool_calls
                     if name == "create_backtest_draft"]),
                0,
            )
            recovery = database.rows(
                "SELECT replacement_reserved,replacement_session_id "
                "FROM direct_execution_recoveries WHERE work_item_id='JOB-1'"
            )[0]
            self.assertEqual(recovery["replacement_reserved"], 1)
            self.assertEqual(recovery["replacement_session_id"], "jesse-session-1")
            self.assertEqual(database.rows(
                "SELECT replacement_created FROM direct_execution_sessions "
                "WHERE work_item_id='JOB-1'"
            )[0]["replacement_created"], 1)

    def test_source_change_gets_one_bounded_preparation_turn(self) -> None:
        fallback = RecordingFallback()
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["finished"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, fallback=fallback)
            result = dispatcher.dispatch(batch_request(change_scope="entry_changed"))
            self.assertEqual(result.payload["results"][0]["outcome"], "finished")
            self.assertEqual(len(fallback.requests), 1)
            self.assertEqual(fallback.requests[0]["task_type"], "prepare_strategies")
            self.assertNotIn("strategy_source", json.dumps(fallback.requests[0]))

    def test_nested_entry_scope_gets_preparation_turn(self) -> None:
        fallback = RecordingFallback()
        request = batch_request()
        experiment = request["requests"][0]["experiment"]
        experiment.pop("change_scope")
        experiment["entry_rule"] = {
            "action": "new",
            "change_scope": "new_entry",
            "description": "Enter on a bounded test condition.",
        }
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["finished"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, fallback=fallback)

            result = dispatcher.dispatch(request)

            self.assertEqual(result.payload["results"][0]["outcome"], "finished")
            self.assertEqual(len(fallback.requests), 1)
            self.assertEqual(fallback.requests[0]["task_type"], "prepare_strategies")

    def test_missing_strategy_becomes_terminal_analysis_result(self) -> None:
        fallback = RecordingFallback()
        fallback.preparation_outcome = "blocked"
        fallback.preparation_readiness = [{
            "work_item_id": "JOB-1",
            "strategy_name": "ExistingStrategy",
            "status": "missing",
            "detail": "Jesse could not discover the named strategy class",
        }]
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["finished"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, fallback=fallback)

            result = dispatcher.dispatch(batch_request(change_scope="entry_changed"))

            item = result.payload["results"][0]
            self.assertEqual(item["outcome"], "blocked")
            self.assertEqual(item["blocker_code"], "source_strategy_not_found")
            self.assertEqual(server.http.run_calls, 0)

    def test_explicit_contract_defect_is_blocked_before_jesse(self) -> None:
        request = batch_request()
        request["requests"][0]["experiment"]["sizing_model"] = (
            "risk_to_qty from starting balance"
        )
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["finished"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server)

            result = dispatcher.dispatch(request)

            item = result.payload["results"][0]
            self.assertEqual(item["outcome"], "blocked")
            self.assertEqual(item["blocker_code"], "strategy_contract_invalid")
            self.assertEqual(server.http.run_calls, 0)

    def test_failed_contract_receipt_blocks_before_jesse(self) -> None:
        fallback = RecordingFallback()
        fallback.preparation_outcome = "blocked"
        fallback.preparation_readiness[0]["status"] = "invalid"
        fallback.preparation_readiness[0]["detail"] = "scalar stop_loss"
        fallback.preparation_readiness[0]["contract_checks"][1] = {
            "code": "exit_shape", "status": "fail",
            "detail": "scalar stop_loss",
        }
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["finished"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, fallback=fallback)

            result = dispatcher.dispatch(batch_request(change_scope="entry_changed"))

            item = result.payload["results"][0]
            self.assertEqual(item["outcome"], "blocked")
            self.assertEqual(item["blocker_code"], "invalid_strategy_preparation")
            self.assertIn("scalar stop_loss", item["detail"])
            self.assertEqual(server.http.run_calls, 0)

    def test_preparation_requires_readiness_evidence(self) -> None:
        fallback = RecordingFallback()
        fallback.preparation_readiness = []
        with tempfile.TemporaryDirectory() as tmp, FakeMcpServer(["finished"]) as server:
            dispatcher, _ = self.make_dispatcher(tmp, server, fallback=fallback)

            result = dispatcher.dispatch(batch_request(change_scope="entry_changed"))

            self.assertEqual(result.outcome, "retry")
            self.assertEqual(result.blocker_code, "invalid_strategy_preparation")
            self.assertEqual(server.http.run_calls, 0)

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
