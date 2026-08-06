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
    classify_jesse_session,
    load_direct_execution_config,
)
from ats_lab.models import ExperimentSpec, WorkItem, WorkState
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
            self.assertEqual(server.http.run_calls, 1)
            telemetry = database.rows(
                "SELECT * FROM direct_execution_telemetry ORDER BY id DESC LIMIT 1"
            )[0]
            self.assertEqual(telemetry["model_call_count"], 0)

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
