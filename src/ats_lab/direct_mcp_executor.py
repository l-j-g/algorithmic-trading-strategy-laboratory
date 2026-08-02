"""Deterministic Jesse Streamable HTTP execution.

Only mechanical draft/start/poll/fetch work lives here. Strategy source remains
inside Jesse and model-backed preparation remains an explicit separate dispatch.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .database import WorkflowDatabase
from .models import utc_now
from .worker import DispatchResult, Dispatcher


TERMINAL = {"finished", "stopped", "failed", "cancelled", "terminated", "failed_to_start"}
SOURCE_CHANGE_SCOPES = {
    "new_entry", "entry_changed", "exit_only", "sizing_only", "risk_only",
    "refactor",
}


class McpError(RuntimeError):
    pass


@dataclass(frozen=True)
class DirectExecutionConfig:
    enabled: bool = False
    mcp_url: str = "http://127.0.0.1:9002/mcp"
    timeout_seconds: float = 60
    poll_initial_seconds: float = 2
    poll_max_seconds: float = 5
    max_polls: int = 3
    dashboard_api_base_url: str = "http://127.0.0.1:9000"
    dashboard_display_base_url: str = "http://127.0.0.1:9000/#/backtest"

    def __post_init__(self) -> None:
        if not self.mcp_url:
            raise ValueError("jesse_executor.mcp_url must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("jesse_executor.timeout_seconds must be positive")
        if self.poll_initial_seconds < 0 or self.poll_max_seconds < 0:
            raise ValueError("jesse_executor polling intervals must be non-negative")
        if self.max_polls < 1:
            raise ValueError("jesse_executor.max_polls must be positive")


def load_direct_execution_config(path: Any) -> DirectExecutionConfig:
    if not path.is_file():
        return DirectExecutionConfig()
    with path.open("rb") as handle:
        section = tomllib.load(handle).get("jesse_executor", {})
    if not isinstance(section, dict):
        raise ValueError("jesse_executor config must be a table")
    allowed = {
        "enabled", "mcp_url", "timeout_seconds", "poll_initial_seconds",
        "poll_max_seconds", "max_polls", "dashboard_api_base_url",
        "dashboard_display_base_url",
    }
    unknown = set(section) - allowed
    if unknown:
        raise ValueError(
            "unknown jesse_executor config: " + ", ".join(sorted(unknown))
        )
    return DirectExecutionConfig(**section)


class McpClient:
    """Small supported Streamable HTTP JSON-RPC client."""

    def __init__(self, url: str, timeout: float = 60) -> None:
        self.url = url
        self.timeout = timeout
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self.next_id = 1
        self.call_count = 0
        self.request_bytes = 0
        self.response_bytes = 0

    def initialize(self) -> None:
        self.post({
            "jsonrpc": "2.0", "id": self._id(), "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "ats-lab-direct-executor", "version": "1"},
            },
        })
        self.post({
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        })

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        response = self.post({
            "jsonrpc": "2.0", "id": self._id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        result = response.get("result") if isinstance(response, dict) else response
        return self._decode_tool_result(result)

    def post(self, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.call_count += 1
        self.request_bytes += len(data)
        request = urllib.request.Request(
            self.url, data=data, headers=self.headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    self.headers["mcp-session-id"] = session_id
        except (urllib.error.URLError, TimeoutError) as error:
            raise McpError(f"MCP transport failed: {error}") from error
        self.response_bytes += len(body)
        decoded = self._decode_http_body(body.decode())
        if isinstance(decoded, dict) and decoded.get("error"):
            raise McpError(str(decoded["error"]))
        return decoded

    def _id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    @staticmethod
    def _decode_http_body(text: str) -> Any:
        for line in text.splitlines():
            if line.startswith("data:"):
                value = line[5:].strip()
                if value:
                    return json.loads(value)
        return json.loads(text) if text.strip() else None

    @staticmethod
    def _decode_tool_result(result: Any) -> Any:
        if not isinstance(result, dict) or not result.get("content"):
            return result
        first = result["content"][0]
        if isinstance(first, dict) and "json" in first:
            return first["json"]
        text = first.get("text") if isinstance(first, dict) else None
        if not isinstance(text, str):
            return result
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


class DashboardClient:
    """Authenticated local Jesse dashboard API fallback. Secrets stay in memory."""

    def __init__(
        self, base_url: str, *, password: str | None = None,
        token: str | None = None, timeout: float = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_environment(
        cls, base_url: str, *, timeout: float = 60,
        environ: dict[str, str] | None = None,
    ) -> DashboardClient | None:
        values = os.environ if environ is None else environ
        token = values.get("JESSE_AUTH_TOKEN")
        password = values.get("JESSE_DASHBOARD_PASSWORD")
        if not token and not password:
            return None
        return cls(base_url, password=password, token=token, timeout=timeout)

    def authenticate(self) -> None:
        if self.token:
            return
        if not self.password:
            raise McpError(
                "dashboard fallback requires JESSE_DASHBOARD_PASSWORD or "
                "JESSE_AUTH_TOKEN in process environment"
            )
        response = self.post(
            "/auth/login", {"password": self.password}, authenticated=False,
        )
        token = response.get("auth_token") if isinstance(response, dict) else None
        if not token:
            raise McpError("dashboard login returned no auth token")
        self.token = str(token)

    def post(
        self, path: str, payload: dict[str, Any], *, authenticated: bool = True,
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if authenticated:
            self.authenticate()
            headers["Authorization"] = str(self.token)
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, separators=(",", ":")).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode()
        except urllib.error.HTTPError as error:
            raise McpError(
                f"dashboard API {path} failed HTTP {error.code}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise McpError(f"dashboard API {path} transport failed: {error}") from error
        return json.loads(body) if body.strip() else {}

    def get_config(self) -> dict[str, Any]:
        response = self.post("/config/get", {"current_config": {}})
        config = response.get("data", {}).get("data") if isinstance(response, dict) else None
        if not isinstance(config, dict) or not isinstance(config.get("backtest"), dict):
            raise McpError("dashboard config response missing backtest config")
        return config

    def get_session(self, session_id: str) -> dict[str, Any]:
        response = self.post(f"/backtest/sessions/{session_id}", {})
        return DirectMcpDispatcher._session(response)

    def run_backtest(self, session_id: str) -> Any:
        session = self.get_session(session_id)
        state = session.get("state") if isinstance(session.get("state"), dict) else {}
        form = state.get("form")
        if not isinstance(form, dict):
            raise McpError(f"dashboard session {session_id} missing form state")
        config = self.get_config()["backtest"]
        payload = {
            "id": session_id,
            "exchange": form["exchange"],
            "routes": form["routes"],
            "data_routes": form.get("data_routes", []),
            "config": config,
            "start_date": form["start_date"],
            "finish_date": form["finish_date"],
            "debug_mode": form.get("debug_mode", False),
            "export_csv": form.get("export_csv", False),
            "export_chart": form.get("export_chart", True),
            "export_tradingview": form.get("export_tradingview", False),
            "export_json": form.get("export_json", False),
            "fast_mode": form.get("fast_mode", False),
            "benchmark": form.get("benchmark", True),
            "theme": "dark",
        }
        return self.post("/backtest", payload)


class DirectMcpDispatcher:
    """Route ordinary backtests directly; delegate preparation/other turns."""

    def __init__(
        self,
        database: WorkflowDatabase,
        config: DirectExecutionConfig,
        *,
        fallback: Dispatcher | None = None,
        sleep: Callable[[float], None] = time.sleep,
        client_factory: Callable[[str, float], McpClient] = McpClient,
        dashboard_client: DashboardClient | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.fallback = fallback
        self.sleep = sleep
        self.client_factory = client_factory
        self.dashboard_client = dashboard_client or DashboardClient.from_environment(
            config.dashboard_api_base_url, timeout=config.timeout_seconds,
        )

    def dispatch(self, request: dict[str, Any]) -> DispatchResult:
        if not self.config.enabled or request.get("task_type") != "execute_batch":
            return self._fallback(request)
        requests = request.get("requests")
        if not isinstance(requests, list):
            return DispatchResult(
                outcome="retry", blocker_code="invalid_direct_batch",
                detail="execute_batch requires requests array",
            )
        direct = [item for item in requests if self._mechanical_backtest(item)]
        delegated = [item for item in requests if item not in direct]
        preparation = [
            item for item in direct
            if self._requires_preparation(item)
            and not self._preparation_complete(item)
        ]
        if preparation:
            prepared = self._fallback({
                "schema_version": 1,
                "task_type": "prepare_strategies",
                "batch_id": request.get("batch_id"),
                "instruction": (
                    "Create or materially edit only required Jesse strategies through "
                    "Jesse MCP. Enforce entry notional <=95% available_margin at 1x. "
                    "Return prepared_work_item_ids. Never return strategy source."
                ),
                "requests": [self._preparation_request(item) for item in preparation],
            })
            if prepared.outcome != "finished":
                return prepared
            payload = prepared.payload or {}
            prepared_ids = payload.get("prepared_work_item_ids")
            expected_ids = [item["work_item_id"] for item in preparation]
            if (
                not isinstance(prepared_ids, list)
                or sorted(prepared_ids) != sorted(expected_ids)
            ):
                return DispatchResult(
                    outcome="retry",
                    blocker_code="invalid_strategy_preparation",
                    detail="preparation must cover every requested work item exactly",
                    payload=payload,
                )
            for item in preparation:
                self._mark_prepared(item)
        results: list[dict[str, Any]] = []
        for item in direct:
            results.append(self._execute_one(item))
        if delegated:
            delegated_result = self._fallback({
                **request, "requests": delegated,
            })
            payload = delegated_result.payload or {}
            fallback_results = payload.get("results")
            if isinstance(fallback_results, list):
                results.extend(fallback_results)
            else:
                for item in delegated:
                    results.append({
                        "work_item_id": item.get("work_item_id"),
                        "outcome": delegated_result.outcome,
                        "blocker_code": delegated_result.blocker_code,
                        "detail": delegated_result.detail,
                    })
        overall = "finished" if all(
            item.get("outcome") in {"finished", "blocked", "retry"} for item in results
        ) else "retry"
        return DispatchResult(
            outcome=overall,
            payload={"outcome": overall, "results": results},
        )

    def _execute_one(self, request: dict[str, Any]) -> dict[str, Any]:
        work_item_id = str(request.get("work_item_id") or "")
        experiment_id = str(request.get("experiment_id") or "")
        client = self.client_factory(self.config.mcp_url, self.config.timeout_seconds)
        polls = 0
        outcome = "retry"
        try:
            client.initialize()
            fingerprint = self._fingerprint(request)
            checkpoint = self._checkpoint(work_item_id)
            if checkpoint and checkpoint["request_fingerprint"] != fingerprint:
                return self._record_and_return(
                    client, work_item_id, polls, "blocked",
                    blocker_code="direct_request_changed",
                    detail="persisted Jesse session request fingerprint changed",
                )
            if checkpoint is None:
                session_id = self._create(client, request)
                self._save_checkpoint(
                    work_item_id, experiment_id, session_id, fingerprint, "draft",
                )
                session = self._start_and_verify(client, session_id)
            else:
                session_id = checkpoint["session_id"]
                if checkpoint["state"] == "finished":
                    metrics = json.loads(checkpoint["metrics_json"] or "{}")
                    return self._finished(client, request, session_id, metrics, polls)
                session = self._session(client.call_tool(
                    "get_backtest_session", {"session_id": session_id},
                ))
                if self._status(session) == "draft":
                    if checkpoint["state"] == "start_recovery_failed":
                        return self._record_and_return(
                            client, work_item_id, polls, "retry",
                            blocker_code="jesse_start_recovery_failed",
                            detail=(
                                f"session {session_id} remains draft after prior "
                                "start recovery"
                            ),
                        )
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint,
                        "draft",
                    )
                    session = self._start_and_verify(client, session_id)
            observed_status = self._status(session)
            self._save_checkpoint(
                work_item_id, experiment_id, session_id, fingerprint,
                observed_status,
            )
            delay = self.config.poll_initial_seconds
            last_status = observed_status
            for polls in range(1, self.config.max_polls + 1):
                if polls > 1 or last_status not in TERMINAL | {"finished"}:
                    session = self._session(client.call_tool(
                        "get_backtest_session", {"session_id": session_id},
                    ))
                    last_status = self._status(session)
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint,
                        last_status,
                    )
                if last_status == "finished":
                    metrics = session.get("metrics")
                    if not isinstance(metrics, dict):
                        return self._record_and_return(
                            client, work_item_id, polls, "retry",
                            blocker_code="invalid_jesse_metrics",
                            detail="terminal Jesse session metrics must be object",
                        )
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint,
                        "finished", metrics=metrics,
                    )
                    return self._finished(
                        client, request, session_id, metrics, polls,
                    )
                if last_status in TERMINAL:
                    detail = str(
                        session.get("exception") or session.get("error")
                        or f"Jesse session terminal status {last_status}"
                    )
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint,
                        last_status, error=detail,
                    )
                    return self._record_and_return(
                        client, work_item_id, polls, "retry",
                        blocker_code=f"jesse_execution_{last_status}", detail=detail,
                    )
                if polls < self.config.max_polls:
                    self.sleep(delay)
                    delay = min(
                        self.config.poll_max_seconds,
                        max(delay * 2, self.config.poll_initial_seconds),
                    )
            outcome = "retry"
            return self._record_and_return(
                client, work_item_id, polls, outcome,
                blocker_code="jesse_poll_timeout",
                detail=f"session {session_id} non-terminal after {polls} polls",
            )
        except (KeyError, TypeError, ValueError, McpError, OSError) as error:
            checkpoint = self._checkpoint(work_item_id)
            if checkpoint and checkpoint["state"] == "draft":
                self._save_checkpoint(
                    work_item_id, experiment_id, checkpoint["session_id"],
                    checkpoint["request_fingerprint"], "start_recovery_failed",
                    error=str(error),
                )
                return self._record_and_return(
                    client, work_item_id, polls, "retry",
                    blocker_code="jesse_start_recovery_failed", detail=str(error),
                )
            return self._record_and_return(
                client, work_item_id, polls, "retry",
                blocker_code="direct_mcp_error", detail=str(error),
            )

    def _start_and_verify(
        self, client: McpClient, session_id: str,
    ) -> dict[str, Any]:
        run = client.call_tool("run_backtest", {"session_id": session_id})
        if not isinstance(run, dict) or run.get("status") != "started":
            raise McpError(f"run_backtest failed for {session_id}: {run}")
        session = self._session(client.call_tool(
            "get_backtest_session", {"session_id": session_id},
        ))
        if self._has_started(session):
            return session
        if self.dashboard_client is None:
            raise McpError(
                f"session {session_id} remained draft after MCP start and dashboard "
                "credentials are unavailable"
            )
        self.dashboard_client.run_backtest(session_id)
        session = self._session(client.call_tool(
            "get_backtest_session", {"session_id": session_id},
        ))
        if not self._has_started(session):
            raise McpError(
                f"session {session_id} remained draft after dashboard start fallback"
            )
        return session

    @classmethod
    def _has_started(cls, session: dict[str, Any]) -> bool:
        state = session.get("state") if isinstance(session.get("state"), dict) else {}
        results = state.get("results") if isinstance(state.get("results"), dict) else {}
        return cls._status(session) != "draft" or results.get("executing") is True

    @staticmethod
    def _status(session: dict[str, Any]) -> str:
        state = session.get("state") if isinstance(session.get("state"), dict) else {}
        return str(session.get("status") or state.get("status") or "unknown")

    def _create(self, client: McpClient, request: dict[str, Any]) -> str:
        experiment = request["experiment"]
        routes = experiment.get("routes")
        if not isinstance(routes, list) or not routes:
            raise ValueError("direct backtest requires experiment.routes")
        windows = {
            (route.get("start_date"), route.get("finish_date"))
            for route in routes if isinstance(route, dict)
        }
        exchanges = {
            route.get("exchange") for route in routes if isinstance(route, dict)
        }
        if len(windows) != 1 or len(exchanges) != 1:
            raise ValueError(
                "direct aggregate backtest requires one shared exchange/date window"
            )
        start_date, finish_date = next(iter(windows))
        strategy = experiment.get("strategy_name")
        mcp_routes = [{
            "exchange": route["exchange"], "strategy": strategy,
            "symbol": route["symbol"], "timeframe": route["timeframe"],
        } for route in routes]
        draft = client.call_tool("create_backtest_draft", {
            "exchange": next(iter(exchanges)),
            "routes": json.dumps(mcp_routes, separators=(",", ":")),
            "data_routes": "[]",
            "start_date": start_date, "finish_date": finish_date,
            "debug_mode": False, "export_csv": False, "export_json": False,
            "export_chart": True, "export_tradingview": False,
            "fast_mode": False, "benchmark": True,
            "title": f"ATS Lab {request['work_item_id']}",
            "description": "ATS Lab deterministic research-only execution.",
        })
        if not isinstance(draft, dict):
            raise McpError(f"create_backtest_draft returned {draft!r}")
        session_id = draft.get("backtest_id") or draft.get("session_id") or draft.get("id")
        if not session_id:
            raise McpError("create_backtest_draft returned no session id")
        return str(session_id)

    def _finished(
        self,
        client: McpClient,
        request: dict[str, Any],
        session_id: str,
        metrics: dict[str, Any],
        polls: int,
    ) -> dict[str, Any]:
        work_item_id = str(request["work_item_id"])
        raw = {
            "session_id": session_id, "status": "finished", "metrics": metrics,
        }
        result = {
            "work_item_id": work_item_id,
            "outcome": "finished",
            "evidence": {"run": {
                "id": f"{work_item_id}:{session_id}",
                "session_id": session_id, "status": "finished",
                "dashboard_url": (
                    f"{self.config.dashboard_display_base_url.rstrip('/')}/{session_id}"
                ),
                "metrics": metrics, "raw_result": raw,
            }},
        }
        self._telemetry(client, work_item_id, "finished", polls, result)
        return result

    def _record_and_return(
        self,
        client: McpClient,
        work_item_id: str,
        polls: int,
        outcome: str,
        *,
        blocker_code: str,
        detail: str,
    ) -> dict[str, Any]:
        result = {
            "work_item_id": work_item_id, "outcome": outcome,
            "blocker_code": blocker_code, "detail": detail,
        }
        self._telemetry(client, work_item_id, outcome, polls, result)
        return result

    def _telemetry(
        self,
        client: McpClient,
        work_item_id: str,
        outcome: str,
        polls: int,
        result: dict[str, Any],
    ) -> None:
        response_bytes = len(json.dumps(
            result, separators=(",", ":"), sort_keys=True,
        ).encode())
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_telemetry(
                       work_item_id,outcome,mcp_call_count,model_call_count,
                       request_bytes,response_bytes,poll_count,recorded_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (
                    work_item_id, outcome, client.call_count, 0,
                    client.request_bytes, client.response_bytes + response_bytes,
                    polls, utc_now(),
                ),
            )

    def _checkpoint(self, work_item_id: str) -> dict[str, Any] | None:
        rows = self.database.rows(
            "SELECT * FROM direct_execution_sessions WHERE work_item_id=?",
            (work_item_id,),
        )
        return rows[0] if rows else None

    def _preparation_complete(self, request: dict[str, Any]) -> bool:
        rows = self.database.rows(
            """SELECT request_fingerprint FROM direct_strategy_preparations
               WHERE work_item_id=?""",
            (request.get("work_item_id"),),
        )
        return bool(rows and rows[0]["request_fingerprint"] == self._fingerprint(request))

    def _mark_prepared(self, request: dict[str, Any]) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_strategy_preparations(
                       work_item_id,request_fingerprint,prepared_at
                   ) VALUES (?,?,?)
                   ON CONFLICT(work_item_id) DO UPDATE SET
                       request_fingerprint=excluded.request_fingerprint,
                       prepared_at=excluded.prepared_at""",
                (
                    request["work_item_id"], self._fingerprint(request), utc_now(),
                ),
            )

    def _save_checkpoint(
        self,
        work_item_id: str,
        experiment_id: str,
        session_id: str,
        fingerprint: str,
        state: str,
        *,
        metrics: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_sessions(
                       work_item_id,experiment_id,session_id,request_fingerprint,
                       state,metrics_json,error_text,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(work_item_id) DO UPDATE SET
                       state=excluded.state,metrics_json=excluded.metrics_json,
                       error_text=excluded.error_text,updated_at=excluded.updated_at""",
                (
                    work_item_id, experiment_id, session_id, fingerprint, state,
                    json.dumps(metrics, separators=(",", ":"), sort_keys=True)
                    if metrics is not None else None,
                    error, now, now,
                ),
            )

    @staticmethod
    def _session(response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise McpError("get_backtest_session returned non-object")
        if response.get("error"):
            raise McpError(str(response["error"]))
        data = response.get("data")
        if isinstance(data, dict) and isinstance(data.get("session"), dict):
            return data["session"]
        if isinstance(response.get("session"), dict):
            return response["session"]
        return response

    @staticmethod
    def _mechanical_backtest(request: dict[str, Any]) -> bool:
        operation = request.get("work_item", {}).get("operation")
        if operation is None:
            operation = {
                "baseline": "backtest", "multi_window": "backtest",
                "cost_sensitivity": "backtest", "out_of_sample": "backtest",
                "harness_check": "backtest",
            }.get(request.get("experiment", {}).get("experiment_type"))
        if operation != "backtest":
            return False
        if request.get("execution_context", {}).get("optimizer_parameters"):
            return False
        experiment = request.get("experiment", {})
        if experiment.get("fee_rate") is not None:
            return False
        routes = experiment.get("routes")
        if not isinstance(routes, list) or not routes:
            return False
        windows = {
            (route.get("start_date"), route.get("finish_date"))
            for route in routes if isinstance(route, dict)
        }
        exchanges = {
            route.get("exchange") for route in routes if isinstance(route, dict)
        }
        return len(windows) == 1 and len(exchanges) == 1

    @staticmethod
    def _requires_preparation(request: dict[str, Any]) -> bool:
        experiment = request.get("experiment", {})
        work = request.get("work_item", {})
        scope = work.get("change_scope") or experiment.get("change_scope")
        return bool(
            work.get("strategy_preparation_required")
            or experiment.get("strategy_preparation_required")
            or scope in SOURCE_CHANGE_SCOPES
        )

    @staticmethod
    def _preparation_request(request: dict[str, Any]) -> dict[str, Any]:
        experiment = request.get("experiment", {})
        work = request.get("work_item", {})
        allowed_experiment = {
            key: experiment.get(key) for key in (
                "strategy_name", "hypothesis", "edge_thesis", "archetype",
                "target_regime", "failure_regime", "entry_rule", "change_scope",
                "sizing_model",
            ) if experiment.get(key) is not None
        }
        return {
            "work_item_id": request.get("work_item_id"),
            "experiment_id": request.get("experiment_id"),
            "experiment": allowed_experiment,
            "work_item": {
                key: work.get(key) for key in (
                    "operation", "controlled_change", "change_scope",
                ) if work.get(key) is not None
            },
        }

    @staticmethod
    def _fingerprint(request: dict[str, Any]) -> str:
        material = {
            "experiment_id": request.get("experiment_id"),
            "strategy_name": request.get("experiment", {}).get("strategy_name"),
            "routes": request.get("experiment", {}).get("routes"),
            "operation": request.get("work_item", {}).get("operation"),
            "execution_context": request.get("execution_context"),
        }
        return hashlib.sha256(json.dumps(
            material, separators=(",", ":"), sort_keys=True,
        ).encode()).hexdigest()

    def _fallback(self, request: dict[str, Any]) -> DispatchResult:
        if self.fallback is None:
            return DispatchResult(
                outcome="retry", blocker_code="direct_fallback_unavailable",
                detail="existing Agent dispatcher is not configured",
            )
        return self.fallback.dispatch(request)
