"""Deterministic Jesse Streamable HTTP execution.

Only mechanical draft/start/poll/fetch work lives here. Strategy source remains
inside Jesse and model-backed preparation remains an explicit separate dispatch.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .database import WorkflowDatabase
from .models import utc_now
from .resources import ResourcePolicy
from .session_recovery import SessionRecoveryPolicy
from .strategy_contracts import StrategyContractValidator
from .worker import DispatchResult, Dispatcher


TERMINAL = {"finished", "stopped", "failed", "cancelled", "terminated", "failed_to_start"}
SOURCE_CHANGE_SCOPES = {
    "new_entry", "entry_changed", "exit_only", "sizing_only", "risk_only",
    "refactor",
}
OPERATION_BY_EXPERIMENT_TYPE = {
    "baseline": "backtest", "multi_window": "backtest",
    "cost_sensitivity": "backtest", "out_of_sample": "backtest",
    "harness_check": "backtest",
}
SIGNIFICANCE_METRIC_FIELDS = (
    "observed_mean", "annualized_return", "p_value",
    "n_simulations", "n_observations",
)
JESSE_OPERATIONS = frozenset({"backtest", "hpo", "significance", "monte_carlo"})
JESSE_RESULT_STATUSES = frozenset(
    {"draft", "running", "finished", "stopped", "terminated", "unknown"},
)
ROUTE_FIELDS = ("exchange", "symbol", "timeframe", "start_date", "finish_date")
RAW_RESULT_KEYS = {"session_id", "status", "metrics"}
MISSING_SESSION_MARKERS = ("session", "not found")


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
    zombie_grace_seconds: float = 60
    zombie_unchanged_observations: int = 2

    def __post_init__(self) -> None:
        if not self.mcp_url:
            raise ValueError("jesse_executor.mcp_url must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("jesse_executor.timeout_seconds must be positive")
        if self.poll_initial_seconds < 0 or self.poll_max_seconds < 0:
            raise ValueError("jesse_executor polling intervals must be non-negative")
        if self.max_polls < 1:
            raise ValueError("jesse_executor.max_polls must be positive")
        if self.zombie_grace_seconds < 0:
            raise ValueError("jesse_executor.zombie_grace_seconds must be non-negative")
        if self.zombie_unchanged_observations < 2:
            raise ValueError(
                "jesse_executor.zombie_unchanged_observations must be at least 2"
            )


@dataclass(frozen=True)
class ExecutionPlan:
    """Jesse MCP tool surface and display routing for one operation kind."""

    operation: str
    create_tool: str
    run_tool: str
    get_tool: str
    dashboard_supported: bool
    dashboard_path: str | None = None

    @classmethod
    def for_request(cls, request: dict[str, Any]) -> "ExecutionPlan":
        operation = DirectMcpDispatcher._operation(request)
        if operation == "significance":
            return cls(
                operation="significance",
                create_tool="create_significance_test_draft",
                run_tool="run_significance_test",
                get_tool="get_significance_test_session",
                dashboard_supported=False,
                dashboard_path="significance-test",
            )
        return cls(
            operation="backtest",
            create_tool="create_backtest_draft",
            run_tool="run_backtest",
            get_tool="get_backtest_session",
            dashboard_supported=True,
            dashboard_path="backtest",
        )


@dataclass(frozen=True)
class SessionClassification:
    state: str
    public_status: str
    executing: bool | None
    progress: float | None
    jesse_updated_at: str | None
    has_execution_evidence: bool
    error: str | None = None


def _results(session: dict[str, Any]) -> dict[str, Any] | None:
    state = session.get("state")
    if not isinstance(state, dict):
        return None
    results = state.get("results")
    return results if isinstance(results, dict) else None


def _progress(results: dict[str, Any]) -> float | None:
    value: Any = results.get("progress")
    progressbar = results.get("progressbar")
    if value is None and isinstance(progressbar, dict):
        value = progressbar.get("current")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _metrics(session: dict[str, Any], results: dict[str, Any] | None) -> Any:
    if "metrics" in session:
        return session.get("metrics")
    return results.get("metrics") if results is not None else None


def _execution_error(
    session: dict[str, Any], results: dict[str, Any] | None,
) -> str | None:
    for value in (session.get("exception"), session.get("error"), session.get("traceback")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = results.get("exception") if results is not None else None
    if isinstance(nested, dict):
        for key in ("error", "traceback"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(nested, str) and nested.strip():
        return nested.strip()
    return None


def _evidence_flags(
    session: dict[str, Any], results: dict[str, Any] | None,
) -> tuple[bool, bool, bool, bool]:
    metrics = _metrics(session, results)
    trades = session.get("trades")
    if trades is None and results is not None:
        trades = results.get("trades")
    equity = session.get("equity_curve")
    charts = results.get("charts") if results is not None else None
    if equity is None and isinstance(charts, dict):
        equity = charts.get("equity_curve")
    duration = session.get("execution_duration")
    return (
        isinstance(metrics, dict) and bool(metrics),
        isinstance(trades, list) and bool(trades),
        isinstance(equity, list) and bool(equity),
        isinstance(duration, (int, float)) and not isinstance(duration, bool),
    )


def classify_jesse_session(
    session: dict[str, Any], *, unchanged_observations: int = 0,
    stale_for_seconds: float = 0, grace_seconds: float = 60,
    required_unchanged_observations: int = 2,
) -> SessionClassification:
    """Classify one bounded Jesse observation without treating timeout as failure."""
    state = session.get("state")
    status = str(
        session.get("status")
        or (state.get("status") if isinstance(state, dict) else "")
        or "unknown"
    ).lower()
    results = _results(session)
    executing = results.get("executing") if results is not None else None
    executing = executing if isinstance(executing, bool) else None
    progress = _progress(results) if results is not None else None
    updated = session.get("updated_at") or session.get("updatedAt")
    updated_text = str(updated) if updated is not None else None
    evidence = _evidence_flags(session, results)
    has_evidence = any(evidence)
    error = _execution_error(session, results)
    base = dict(
        public_status=status, executing=executing, progress=progress,
        jesse_updated_at=updated_text, has_execution_evidence=has_evidence,
        error=error,
    )
    if status == "finished":
        metrics = _metrics(session, results)
        return SessionClassification(
            state="terminal_success" if isinstance(metrics, dict)
            else "malformed_session", **base,
        )
    if status in TERMINAL:
        if status == "draft":
            return SessionClassification(state="draft_not_started", **base)
        return SessionClassification(state="terminal_failure", **base)
    if status == "draft":
        return SessionClassification(state="draft_not_started", **base)
    if results is None or executing is None:
        return SessionClassification(state="malformed_session", **base)
    if executing:
        return SessionClassification(state="active_execution", **base)
    if error:
        return SessionClassification(state="terminal_failure", **base)
    if status == "running" and not has_evidence and (progress is None or progress == 0):
        proven = (
            unchanged_observations >= required_unchanged_observations
            and stale_for_seconds >= grace_seconds
        )
        return SessionClassification(
            state="zombie_nonexecuting" if proven else "temporarily_nonterminal",
            **base,
        )
    if status in {"running", "pending", "queued", "starting"}:
        return SessionClassification(state="temporarily_nonterminal", **base)
    return SessionClassification(state="malformed_session", **base)


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _json_safe(value: Any) -> Any:
    """Replace non-finite floats with null so json.dumps stays strict JSON."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _route_violations(routes: Any) -> list[str]:
    if not isinstance(routes, list) or not routes:
        return ["experiment.routes must be a non-empty array"]
    violations: list[str] = []
    for index, route in enumerate(routes):
        if not isinstance(route, dict):
            violations.append(f"experiment.routes[{index}] must be an object")
            continue
        for field in ROUTE_FIELDS:
            if not _text(route.get(field)):
                violations.append(
                    f"experiment.routes[{index}].{field} must be non-empty text"
                )
    return violations


def _data_route_violations(data_routes: Any, label: str) -> list[str]:
    if data_routes is None:
        return []
    if not isinstance(data_routes, list):
        return [f"{label} must be an array"]
    violations: list[str] = []
    for index, route in enumerate(data_routes):
        if not isinstance(route, dict):
            violations.append(f"{label}[{index}] must be an object")
            continue
        for field in ("exchange", "symbol", "timeframe"):
            if not _text(route.get(field)):
                violations.append(
                    f"{label}[{index}].{field} must be non-empty text"
                )
    return violations


def _gate_violations(label: str, gates: Any) -> list[str]:
    if gates is None:
        return []
    if not isinstance(gates, list):
        return [f"{label} must be an array"]
    violations: list[str] = []
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            violations.append(f"{label}[{index}] must be an object")
            continue
        for field in ("name", "operator"):
            if not _text(gate.get(field)):
                violations.append(f"{label}[{index}].{field} must be non-empty text")
        if "required" in gate and not isinstance(gate["required"], bool):
            violations.append(f"{label}[{index}].required must be boolean")
    return violations


def execution_request_violations(request: Any) -> list[str]:
    """Report jesse-execution-request schema violations for one workflow item.

    Hand-rolled projection of ``schemas/jesse-execution-request.schema.json``
    onto the nested workflow item the direct executor receives: identity,
    operation enum, strategy name, routes, parameters, and gate shapes.
    Batch-level identity fields (``request_id``, ``transport``) are owned by
    the batch envelope and are not repeated per work item.
    """
    if not isinstance(request, dict):
        return ["execution request must be an object"]
    violations: list[str] = []
    if "schema_version" in request and request["schema_version"] != 1:
        violations.append("schema_version must be 1")
    for field in ("work_item_id", "experiment_id"):
        if not _text(request.get(field)):
            violations.append(f"{field} must be non-empty text")
    experiment = request.get("experiment")
    work_item = request.get("work_item")
    if not isinstance(experiment, dict):
        violations.append("experiment must be an object")
    else:
        if not _text(experiment.get("strategy_name")):
            violations.append("experiment.strategy_name must be non-empty text")
        violations.extend(_route_violations(experiment.get("routes")))
        violations.extend(_data_route_violations(
            experiment.get("data_routes"), "experiment.data_routes",
        ))
        violations.extend(_gate_violations(
            "experiment.success_gates", experiment.get("success_gates"),
        ))
        violations.extend(_gate_violations(
            "experiment.failure_gates", experiment.get("failure_gates"),
        ))
    if not isinstance(work_item, dict):
        violations.append("work_item must be an object")
    else:
        operation = work_item.get("operation")
        if operation is not None and operation not in JESSE_OPERATIONS:
            violations.append(
                "work_item.operation must be one of "
                + ", ".join(sorted(JESSE_OPERATIONS))
            )
        parameters = work_item.get("parameters")
        if parameters is not None and not isinstance(parameters, dict):
            violations.append("work_item.parameters must be an object")
        violations.extend(_data_route_violations(
            work_item.get("data_routes"), "work_item.data_routes",
        ))
    return violations


def execution_result_violations(view: Any) -> list[str]:
    """Report jesse-execution-result schema violations for one result view.

    Hand-rolled projection of ``schemas/jesse-execution-result.schema.json``.
    ``raw_result`` must keep exactly session_id/status/metrics with values
    equal to the outer envelope, mirroring the supervisor persistence check.
    """
    if not isinstance(view, dict):
        return ["execution result must be an object"]
    violations: list[str] = []
    if view.get("schema_version") != 1:
        violations.append("schema_version must be 1")
    if view.get("transport") != "jesse_mcp":
        violations.append("transport must be jesse_mcp")
    if not _text(view.get("work_item_id")):
        violations.append("work_item_id must be non-empty text")
    status = view.get("status")
    if status not in JESSE_RESULT_STATUSES:
        violations.append(
            "status must be one of " + ", ".join(sorted(JESSE_RESULT_STATUSES))
        )
    session_id = view.get("session_id")
    if not _text(session_id):
        violations.append("session_id must be non-empty text")
    dashboard_url = view.get("dashboard_url")
    if dashboard_url is not None and not isinstance(dashboard_url, str):
        violations.append("dashboard_url must be text or null")
    if not isinstance(view.get("metrics"), dict):
        violations.append("metrics must be an object")
    if view.get("error") is not None:
        violations.append("finished result cannot contain an error")
    raw = view.get("raw_result")
    if not isinstance(raw, dict) or set(raw) != RAW_RESULT_KEYS:
        violations.append(
            "raw_result must contain exactly session_id, status, and metrics"
        )
    else:
        if raw.get("session_id") != session_id:
            violations.append("raw_result.session_id must equal session_id")
        if raw.get("status") != status:
            violations.append("raw_result.status must equal status")
        if raw.get("metrics") != view.get("metrics"):
            violations.append("raw_result.metrics must equal metrics")
    return violations


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
        "zombie_grace_seconds", "zombie_unchanged_observations",
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
        frames = self._decode_http_body(body.decode())
        decoded = self._select_response(frames, payload.get("id"))
        if isinstance(decoded, dict) and decoded.get("error"):
            raise McpError(str(decoded["error"]))
        return decoded

    def close(self) -> None:
        """Best-effort Streamable HTTP DELETE teardown of the MCP session."""
        session_id = self.headers.get("mcp-session-id")
        if not session_id:
            return
        headers = {
            key: value for key, value in self.headers.items()
            if key != "Content-Type"
        }
        request = urllib.request.Request(
            self.url, headers=headers, method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                pass
        except (urllib.error.URLError, OSError, TimeoutError):
            pass

    def _id(self) -> int:
        value = self.next_id
        self.next_id += 1
        return value

    @staticmethod
    def _decode_http_body(text: str) -> list[Any]:
        frames: list[str] = []
        for line in text.splitlines():
            if line.startswith("data:"):
                value = line[5:].strip()
                if value:
                    frames.append(value)
        if not frames and text.strip():
            frames.append(text)
        return [json.loads(frame) for frame in frames]

    @staticmethod
    def _select_response(frames: list[Any], request_id: Any) -> Any:
        """Correlate the JSON-RPC response id and discard foreign frames.

        Interleaved notifications and responses belonging to other requests
        are ignored; only a frame whose ``id`` equals the request ``id`` is
        accepted. A response stream that never carries the matching id is a
        transport defect and surfaces as :class:`McpError`, which the bounded
        executor poll slice treats like any other retryable transport error.
        """
        if request_id is None:
            return frames[0] if frames else None
        matching = [
            frame for frame in frames
            if isinstance(frame, dict)
            and "id" in frame
            and frame.get("id") == request_id
        ]
        if matching:
            return matching[-1]
        observed = [
            frame.get("id") for frame in frames if isinstance(frame, dict)
        ]
        raise McpError(
            f"MCP response id mismatch: expected {request_id!r}, "
            f"received {observed!r}"
        )

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

    def get_session(self, session_id: str) -> dict[str, Any]:
        response = self.post(f"/backtest/sessions/{session_id}", {})
        return DirectMcpDispatcher._session(response)

    def run_backtest(self, session_id: str) -> Any:
        session = self.get_session(session_id)
        state = session.get("state") if isinstance(session.get("state"), dict) else {}
        form = state.get("form")
        if not isinstance(form, dict):
            raise McpError(f"dashboard session {session_id} missing form state")
        payload = {
            "id": session_id,
            "exchange": form["exchange"],
            "routes": form["routes"],
            "data_routes": form.get("data_routes", []),
            "config": form.get("config", {}),
            "balance": form.get("balance"),
            "fee": form.get("fee"),
            "futures_leverage": form.get("futures_leverage"),
            "futures_leverage_mode": form.get("futures_leverage_mode"),
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
        contract_validator: StrategyContractValidator | None = None,
        resource_policy: ResourcePolicy | None = None,
    ) -> None:
        self.database = database
        self.config = config
        self.fallback = fallback
        self.sleep = sleep
        self.client_factory = client_factory
        self.session_recovery_policy = SessionRecoveryPolicy()
        self.resource_policy = resource_policy or ResourcePolicy()
        self.dashboard_client = dashboard_client or DashboardClient.from_environment(
            config.dashboard_api_base_url, timeout=config.timeout_seconds,
        )
        self.contract_validator = contract_validator or StrategyContractValidator()

    def dispatch(self, request: dict[str, Any]) -> DispatchResult:
        if not self.config.enabled or request.get("task_type") != "execute_batch":
            return self._fallback(request)
        requests = request.get("requests")
        if not isinstance(requests, list):
            return DispatchResult(
                outcome="retry", blocker_code="invalid_direct_batch",
                detail="execute_batch requires requests array",
            )
        direct: list[dict[str, Any]] = []
        delegated: list[dict[str, Any]] = []
        for item in requests:
            if self._mechanical_backtest(item):
                direct.append(item)
            else:
                delegated.append(item)
        results: list[dict[str, Any]] = []
        contract_valid: list[dict[str, Any]] = []
        for item in direct:
            issues = self.contract_validator.validate_request(item)
            if issues:
                results.append({
                    "work_item_id": item.get("work_item_id"),
                    "outcome": "blocked",
                    "blocker_code": "strategy_contract_invalid",
                    "detail": "; ".join(
                        f"{issue.code}: {issue.detail}" for issue in issues
                    )[:1000],
                })
            else:
                contract_valid.append(item)
        direct = contract_valid
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
                    "Jesse MCP. Enforce entry notional <=95% available_margin * "
                    "session_leverage; if fixed L_max is declared, session_leverage "
                    "must not exceed it and L_max is not an HPO parameter. "
                    "Return prepared_work_item_ids. Never return strategy source."
                ),
                "requests": [self._preparation_request(item) for item in preparation],
            })
            if prepared.outcome not in {"finished", "blocked"}:
                return prepared
            payload = prepared.payload or {}
            readiness, readiness_error = self._strategy_readiness(
                payload, preparation,
            )
            if readiness_error:
                return DispatchResult(
                    outcome="retry",
                    blocker_code="invalid_strategy_preparation",
                    detail=readiness_error,
                    payload=payload,
                )
            ready_ids = {
                work_item_id for work_item_id, status in readiness.items()
                if status["status"] == "ready"
            }
            nonready_ids = set(readiness) - ready_ids
            if prepared.outcome == "finished" and nonready_ids:
                return DispatchResult(
                    outcome="retry",
                    blocker_code="invalid_strategy_preparation",
                    detail="finished preparation cannot contain non-ready strategies",
                    payload=payload,
                )
            if prepared.outcome == "blocked" and not nonready_ids:
                return DispatchResult(
                    outcome="retry",
                    blocker_code="invalid_strategy_preparation",
                    detail="blocked preparation must identify a non-ready strategy",
                    payload=payload,
                )
            prepared_ids = payload.get("prepared_work_item_ids")
            if (
                not isinstance(prepared_ids, list)
                or not all(isinstance(item_id, str) for item_id in prepared_ids)
                or sorted(prepared_ids) != sorted(ready_ids)
            ):
                return DispatchResult(
                    outcome="retry",
                    blocker_code="invalid_strategy_preparation",
                    detail=(
                        "prepared_work_item_ids must exactly match strategy "
                        "readiness entries marked ready"
                    ),
                    payload=payload,
                )
            for item in preparation:
                status = readiness[item["work_item_id"]]
                if status["status"] == "ready":
                    self._mark_prepared(item)
                    continue
                results.append({
                    "work_item_id": item["work_item_id"],
                    "outcome": "blocked",
                    "blocker_code": (
                        "source_strategy_not_found"
                        if status["status"] == "missing"
                        else "invalid_strategy_preparation"
                    ),
                    "detail": status["detail"],
                })
        for item in direct:
            if any(
                result.get("work_item_id") == item.get("work_item_id")
                for result in results
            ):
                continue
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
        return DispatchResult(
            outcome="finished",
            payload={"outcome": "finished", "results": results},
        )

    def _strategy_readiness(
        self, payload: dict[str, Any], preparation: list[dict[str, Any]],
    ) -> tuple[dict[str, dict[str, str]], str | None]:
        """Validate model-reported Jesse strategy discoverability.

        Preparation is a separate model turn, so its success envelope must
        carry bounded readiness evidence before direct execution is allowed.
        Missing or invalid classes become per-item terminal research failures;
        malformed evidence remains a retryable contract failure.
        """
        entries = payload.get("strategy_readiness")
        if not isinstance(entries, list):
            return {}, "preparation requires strategy_readiness array"
        expected = {
            str(item["work_item_id"]): item for item in preparation
        }
        by_id: dict[str, dict[str, str]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                return {}, "strategy_readiness entries must be objects"
            work_item_id = entry.get("work_item_id")
            status = entry.get("status")
            if not isinstance(work_item_id, str) or work_item_id not in expected:
                return {}, "strategy_readiness must cover requested work items exactly"
            if work_item_id in by_id:
                return {}, "strategy_readiness contains duplicate work item"
            if status not in {"ready", "missing", "invalid"}:
                return {}, "strategy_readiness status must be ready, missing, or invalid"
            expected_strategy = expected[work_item_id].get("experiment", {}).get(
                "strategy_name"
            )
            reported_strategy = entry.get("strategy_name")
            if (
                isinstance(expected_strategy, str) and expected_strategy
                and reported_strategy != expected_strategy
            ):
                return {}, "strategy_readiness strategy_name does not match request"
            detail = " ".join(str(entry.get("detail") or "").split())[:1000]
            validation = self.contract_validator.validate_readiness(entry)
            if validation.malformed:
                return {}, f"strategy_readiness {work_item_id}: {validation.detail}"
            status = validation.status
            detail = validation.detail
            by_id[work_item_id] = {
                "status": status,
                "detail": detail or "Jesse strategy is discoverable and loadable",
            }
        if set(by_id) != set(expected):
            return {}, "strategy_readiness must cover every requested work item exactly"
        return by_id, None

    def _execute_one(self, request: dict[str, Any]) -> dict[str, Any]:
        work_item_id = str(request.get("work_item_id") or "")
        experiment_id = str(request.get("experiment_id") or "")
        plan = ExecutionPlan.for_request(request)
        client = self.client_factory(self.config.mcp_url, self.config.timeout_seconds)
        polls = 0
        outcome = "retry"
        try:
            violations = execution_request_violations(request)
            if violations:
                raise McpError(
                    "jesse-execution-request schema violation: "
                    + "; ".join(violations)
                )
            client.initialize()
            fingerprint = self._fingerprint(request)
            checkpoint = self._checkpoint(work_item_id)
            if checkpoint and checkpoint["request_fingerprint"] != fingerprint:
                return self._record_and_return(
                    client, work_item_id, polls, "blocked",
                    blocker_code="direct_request_changed",
                    detail="persisted Jesse session request fingerprint changed",
                )
            if checkpoint is not None:
                self._adopt_replacement_checkpoint(
                    work_item_id, str(checkpoint["session_id"]),
                )
            if checkpoint is None:
                session_id, replacement, created_now = self._create_or_resume_session(
                    client, request, plan,
                )
                try:
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint, "draft",
                    )
                except Exception:
                    self._record_orphaned_draft(
                        work_item_id, experiment_id, session_id,
                    )
                    raise
                if replacement:
                    with self.database.connect() as connection:
                        connection.execute(
                            """UPDATE direct_execution_sessions
                               SET replacement_created=1 WHERE work_item_id=?""",
                            (work_item_id,),
                        )
                if created_now:
                    session = self._start_and_verify(client, session_id, plan)
                else:
                    session = self._fetch_session(client, plan, session_id)
                    if self._status(session) == "draft":
                        session = self._start_and_verify(client, session_id, plan)
            else:
                session_id = checkpoint["session_id"]
                if checkpoint["state"] in {"finished", "terminal_success"}:
                    metrics = json.loads(checkpoint["metrics_json"] or "{}")
                    return self._finished(
                        client, request, session_id, metrics, polls, plan,
                    )
                session = self._fetch_session(client, plan, session_id)
                if self._status(session) == "draft":
                    if checkpoint["state"] == "start_recovery_failed":
                        return self._record_and_return(
                            client, work_item_id, polls, "blocked",
                            blocker_code="jesse_start_recovery_failed",
                            detail=(
                                f"session {session_id} remains draft after prior "
                                "start recovery; bounded session recovery exhausted "
                                "and requires strategy or harness analysis"
                            ),
                            attempt_charged=True,
                        )
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint,
                        "draft",
                    )
                    session = self._start_and_verify(client, session_id, plan)
            classification = self._observe_session(
                work_item_id, experiment_id, session_id, fingerprint, session,
            )
            delay = self.config.poll_initial_seconds
            for polls in range(1, self.config.max_polls + 1):
                if polls > 1 or classification.state not in {
                    "terminal_success", "terminal_failure",
                }:
                    session = self._fetch_session(client, plan, session_id)
                    classification = self._observe_session(
                        work_item_id, experiment_id, session_id, fingerprint,
                        session,
                    )
                if classification.state == "terminal_success":
                    metrics = _json_safe(_metrics(session, _results(session)))
                    if not isinstance(metrics, dict):
                        return self._record_and_return(
                            client, work_item_id, polls, "retry",
                            blocker_code="invalid_jesse_metrics",
                            detail="terminal Jesse session metrics must be object",
                            attempt_charged=False,
                        )
                    if (
                        plan.operation == "significance"
                        and not self._significance_metrics_complete(metrics)
                    ):
                        return self._record_and_return(
                            client, work_item_id, polls, "retry",
                            blocker_code="invalid_jesse_metrics",
                            detail=(
                                "significance terminal metrics must include "
                                + ", ".join(SIGNIFICANCE_METRIC_FIELDS)
                            ),
                            attempt_charged=False,
                        )
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint,
                        "terminal_success", metrics=metrics,
                    )
                    return self._finished(
                        client, request, session_id, metrics, polls, plan,
                    )
                if classification.state == "terminal_failure":
                    detail = classification.error or (
                        "Jesse session terminal status "
                        f"{classification.public_status}"
                    )
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint,
                        "terminal_failure", error=detail,
                    )
                    return self._record_and_return(
                        client, work_item_id, polls, "blocked",
                        blocker_code=(
                            f"jesse_execution_{classification.public_status}"
                        ),
                        detail=detail,
                    )
                if classification.state == "malformed_session":
                    checkpoint = self._checkpoint(work_item_id) or {}
                    if self.session_recovery_policy.exhausted(
                        classification.state,
                        recovery_attempted=bool(checkpoint.get("recovery_attempted")),
                    ):
                        return self._record_and_return(
                            client, work_item_id, polls, "blocked",
                            blocker_code="malformed_jesse_session",
                            detail=(
                                f"session {session_id} remained malformed after one "
                                "bounded reconciliation; pass execution evidence to "
                                "strategy or harness analysis"
                            ),
                            attempt_charged=True,
                        )
                    self._mark_recovery_attempted(work_item_id)
                    return self._record_and_return(
                        client, work_item_id, polls, "retry",
                        blocker_code="malformed_jesse_session",
                        detail=(
                            f"session {session_id} response lacks required execution "
                            "state; one bounded reconciliation will be attempted"
                        ),
                        attempt_charged=False,
                    )
                if classification.state == "draft_not_started":
                    return self._record_and_return(
                        client, work_item_id, polls, "retry",
                        blocker_code="jesse_draft_not_started",
                        detail=f"session {session_id} has not started",
                        attempt_charged=False,
                    )
                if polls < self.config.max_polls:
                    self.sleep(delay)
                    delay = min(
                        self.config.poll_max_seconds,
                        max(delay * 2, self.config.poll_initial_seconds),
                    )
            if classification.state == "zombie_nonexecuting":
                checkpoint = self._checkpoint(work_item_id) or {}
                if not checkpoint.get("recovery_attempted"):
                    self._mark_recovery_attempted(work_item_id)
                    client.call_tool(plan.run_tool, {"session_id": session_id})
                    return self._record_and_return(
                        client, work_item_id, polls, "retry",
                        blocker_code="jesse_zombie_recovery_pending",
                        detail=(
                            f"session {session_id} non-executing; one start "
                            "reconciliation requested"
                        ),
                        attempt_charged=False,
                    )
                return self._record_and_return(
                    client, work_item_id, polls, "blocked",
                    blocker_code="jesse_zombie_recovery_required",
                    detail=(
                        f"session {session_id} remains non-executing after one "
                        "reconciliation; bounded session recovery exhausted and "
                        "requires strategy or harness analysis"
                    ),
                    attempt_charged=True,
                )
            return self._record_and_return(
                client, work_item_id, polls, "retry",
                blocker_code="jesse_execution_deferred",
                detail=(
                    f"session {session_id} {classification.state} after "
                    f"bounded {polls}-poll slice"
                ),
                attempt_charged=False,
            )
        except (KeyError, TypeError, ValueError, McpError, OSError) as error:
            checkpoint = self._checkpoint(work_item_id)
            if checkpoint:
                recovery_status = self._recover_missing_session_checkpoint(
                    work_item_id, experiment_id, checkpoint, error,
                )
                if recovery_status in {"recovered", "already_registered"}:
                    return self._record_and_return(
                        client, work_item_id, polls, "retry",
                        blocker_code="jesse_session_recovery_pending",
                        detail=(
                            f"session {checkpoint['session_id']} is no longer "
                            "present in Jesse; one replacement session will be "
                            "created on the next attempt"
                        ),
                        attempt_charged=False,
                    )
                if recovery_status == "replacement_exhausted":
                    return self._record_and_return(
                        client, work_item_id, polls, "blocked",
                        blocker_code="jesse_session_recovery_exhausted",
                        detail=(
                            f"session {checkpoint['session_id']} is missing after "
                            "the one allowed replacement; requires bounded "
                            "Jesse or harness analysis"
                        ),
                        attempt_charged=True,
                    )
            if checkpoint and checkpoint["state"] == "draft":
                self._save_checkpoint(
                    work_item_id, experiment_id, checkpoint["session_id"],
                    checkpoint["request_fingerprint"], "start_recovery_failed",
                    error=str(error),
                )
                return self._record_and_return(
                    client, work_item_id, polls, "retry",
                    blocker_code="jesse_start_recovery_failed", detail=str(error),
                    attempt_charged=False,
                )
            return self._record_and_return(
                client, work_item_id, polls, "retry",
                blocker_code="direct_mcp_error", detail=str(error),
                attempt_charged=False,
            )
        finally:
            client.close()

    def _start_and_verify(
        self, client: McpClient, session_id: str, plan: ExecutionPlan,
    ) -> dict[str, Any]:
        run = client.call_tool(plan.run_tool, {"session_id": session_id})
        if not isinstance(run, dict) or run.get("status") != "started":
            raise McpError(f"{plan.run_tool} failed for {session_id}: {run}")
        session = self._fetch_session(client, plan, session_id)
        if self._has_started(session):
            return session
        tolerated = self._await_asynchronous_start(client, plan, session_id)
        if tolerated is not None:
            return tolerated
        if not plan.dashboard_supported:
            raise McpError(
                f"session {session_id} remained draft after MCP start and "
                "dashboard start fallback is not supported for this operation kind"
            )
        if self.dashboard_client is None:
            raise McpError(
                f"session {session_id} remained draft after MCP start and dashboard "
                "credentials are unavailable"
            )
        self.dashboard_client.run_backtest(session_id)
        session = self._fetch_session(client, plan, session_id)
        if self._has_started(session):
            return session
        tolerated = self._await_asynchronous_start(client, plan, session_id)
        if tolerated is not None:
            return tolerated
        raise McpError(
            f"session {session_id} remained draft after dashboard start fallback"
        )

    def _await_asynchronous_start(
        self, client: McpClient, plan: ExecutionPlan, session_id: str,
    ) -> dict[str, Any] | None:
        """Tolerate a start landing after run_* accepted but before it shows.

        The dashboard fallback must not restart a session whose MCP start is
        still landing asynchronously, so a draft observation is re-checked
        once after one poll interval before any second start is issued.
        """
        grace = self.config.poll_initial_seconds
        if grace <= 0:
            return None
        self.sleep(grace)
        session = self._fetch_session(client, plan, session_id)
        return session if self._has_started(session) else None

    @classmethod
    def _has_started(cls, session: dict[str, Any]) -> bool:
        state = session.get("state") if isinstance(session.get("state"), dict) else {}
        results = state.get("results") if isinstance(state.get("results"), dict) else {}
        return cls._status(session) != "draft" or results.get("executing") is True

    @staticmethod
    def _status(session: dict[str, Any]) -> str:
        state = session.get("state") if isinstance(session.get("state"), dict) else {}
        return str(session.get("status") or state.get("status") or "unknown")

    @staticmethod
    def _shared_route_window(
        experiment: dict[str, Any], label: str,
    ) -> tuple[str, str, str, list[dict[str, Any]]]:
        routes = experiment.get("routes")
        if not isinstance(routes, list) or not routes:
            raise ValueError(f"direct {label} requires experiment.routes")
        windows = {
            (route.get("start_date"), route.get("finish_date"))
            for route in routes if isinstance(route, dict)
        }
        exchanges = {
            route.get("exchange") for route in routes if isinstance(route, dict)
        }
        if len(windows) != 1 or len(exchanges) != 1:
            raise ValueError(
                f"direct {label} requires one shared exchange/date window"
            )
        start_date, finish_date = next(iter(windows))
        strategy = experiment.get("strategy_name")
        mcp_routes = [{
            "exchange": route["exchange"], "strategy": strategy,
            "symbol": route["symbol"], "timeframe": route["timeframe"],
        } for route in routes]
        return next(iter(exchanges)), start_date, finish_date, mcp_routes

    def _create(
        self, client: McpClient, request: dict[str, Any], plan: ExecutionPlan,
    ) -> str:
        if plan.operation == "significance":
            return self._create_significance(client, request)
        exchange, start_date, finish_date, mcp_routes = (
            self._shared_route_window(request["experiment"], "backtest")
        )
        work_item = request.get("work_item", {})
        experiment_data_routes = request["experiment"].get("data_routes", [])
        work_data_routes = work_item.get("data_routes", [])
        data_routes = work_data_routes or experiment_data_routes or []
        if not isinstance(data_routes, list):
            raise ValueError("data_routes must be a list")
        session_config = self._session_exchange_config(request["experiment"])
        draft = client.call_tool("create_backtest_draft", {
            "exchange": exchange,
            "routes": json.dumps(mcp_routes, separators=(",", ":")),
            "data_routes": json.dumps(data_routes, separators=(",", ":")),
            "start_date": start_date, "finish_date": finish_date,
            "debug_mode": False, "export_csv": False, "export_json": False,
            "export_chart": True, "export_tradingview": False,
            "fast_mode": False, "benchmark": True,
            **session_config,
            "title": f"ATS Lab {request['work_item_id']}",
            "description": "ATS Lab deterministic research-only execution.",
        })
        if not isinstance(draft, dict):
            raise McpError(f"create_backtest_draft returned {draft!r}")
        session_id = draft.get("backtest_id") or draft.get("session_id") or draft.get("id")
        if not session_id:
            raise McpError("create_backtest_draft returned no session id")
        return str(session_id)

    def _create_significance(self, client: McpClient, request: dict[str, Any]) -> str:
        exchange, start_date, finish_date, mcp_routes = (
            self._shared_route_window(request["experiment"], "significance test")
        )
        work_item = request.get("work_item", {})
        parameters = work_item.get("parameters") or {}
        experiment_data_routes = request["experiment"].get("data_routes", [])
        work_data_routes = work_item.get("data_routes", [])
        data_routes = work_data_routes or experiment_data_routes or []
        if not isinstance(data_routes, list):
            raise ValueError("data_routes must be a list")
        n_simulations = parameters.get("n_simulations")
        if n_simulations is None:
            raise ValueError(
                "significance work item requires parameters.n_simulations"
            )
        payload: dict[str, Any] = {
            "exchange": exchange,
            "routes": json.dumps(mcp_routes, separators=(",", ":")),
            "data_routes": json.dumps(data_routes, separators=(",", ":")),
            "start_date": start_date, "finish_date": finish_date,
            "n_simulations": n_simulations,
            "debug_mode": False, "export_csv": False, "export_json": False,
            "export_chart": True, "export_tradingview": False,
            "fast_mode": False, "benchmark": True,
            **self._session_exchange_config(request["experiment"]),
            "title": f"ATS Lab significance {request['work_item_id']}",
            "description": "ATS Lab deterministic significance-test execution.",
        }
        if parameters.get("random_seed") is not None:
            payload["random_seed"] = parameters["random_seed"]
        draft = client.call_tool("create_significance_test_draft", payload)
        if not isinstance(draft, dict):
            raise McpError(f"create_significance_test_draft returned {draft!r}")
        session_id = (
            draft.get("significance_test_id") or draft.get("backtest_id")
            or draft.get("session_id") or draft.get("id")
        )
        if not session_id:
            raise McpError("create_significance_test_draft returned no session id")
        return str(session_id)

    @staticmethod
    def _session_exchange_config(experiment: dict[str, Any]) -> dict[str, Any]:
        """Return an explicit, immutable exchange snapshot for each draft."""
        # Work-item/experiment JSON may contain explicit nulls for optional
        # fields. ``dict.get(key, default)`` does not fall back in that case,
        # and Jesse's draft schema rejects null for all four settings.
        def first_non_null(*keys: str, default: Any) -> Any:
            for key in keys:
                value = experiment.get(key)
                if value is not None:
                    return value
            return default

        balance = first_non_null(
            "balance", "starting_balance", default=10_000,
        )
        fee = first_non_null("fee_rate", "fee", default=0.0005)
        leverage = first_non_null(
            "futures_leverage", "leverage", default=1,
        )
        leverage_mode = first_non_null(
            "futures_leverage_mode", "leverage_mode", default="cross",
        )
        return {
            "balance": balance,
            "fee": fee,
            "futures_leverage": leverage,
            "futures_leverage_mode": leverage_mode,
        }

    def _create_or_resume_session(
        self, client: McpClient, request: dict[str, Any], plan: ExecutionPlan,
    ) -> tuple[str, bool, bool]:
        work_item_id = str(request["work_item_id"])
        rows = self.database.rows(
            """SELECT replacement_allowed,replacement_reserved,
                      replacement_session_id
               FROM direct_execution_recoveries WHERE work_item_id=?""",
            (work_item_id,),
        )
        if not rows:
            return self._create(client, request, plan), False, True
        recovery = rows[0]
        if recovery["replacement_session_id"]:
            return str(recovery["replacement_session_id"]), True, False
        if not recovery["replacement_allowed"]:
            raise McpError("replacement session is not allowed")
        if recovery["replacement_reserved"]:
            raise McpError(
                "replacement reservation has no persisted session id; manual reconciliation required"
            )
        now = utc_now()
        with self.database.connect() as connection:
            reserved = connection.execute(
                """UPDATE direct_execution_recoveries
                   SET replacement_reserved=1,updated_at=?
                   WHERE work_item_id=? AND replacement_allowed=1
                     AND replacement_reserved=0 AND replacement_session_id IS NULL""",
                (now, work_item_id),
            )
            if reserved.rowcount != 1:
                raise McpError("replacement session reservation changed")
        try:
            session_id = self._create(client, request, plan)
        except Exception:
            self._release_replacement_reservation(work_item_id)
            raise
        with self.database.connect() as connection:
            saved = connection.execute(
                """UPDATE direct_execution_recoveries
                   SET replacement_session_id=?,updated_at=?
                   WHERE work_item_id=? AND replacement_reserved=1
                     AND replacement_session_id IS NULL""",
                (session_id, utc_now(), work_item_id),
            )
            if saved.rowcount != 1:
                raise McpError("replacement session id persistence failed")
        return session_id, True, True

    def _release_replacement_reservation(self, work_item_id: str) -> None:
        """Undo a reservation whose replacement draft never got created.

        A crash between reservation and session-id persist still wedges the
        row; :mod:`ats_lab.correctness_recovery` clears those orphans.
        """
        with self.database.connect() as connection:
            connection.execute(
                """UPDATE direct_execution_recoveries
                   SET replacement_reserved=0,updated_at=?
                   WHERE work_item_id=? AND replacement_reserved=1
                     AND replacement_session_id IS NULL""",
                (utc_now(), work_item_id),
            )

    def _adopt_replacement_checkpoint(
        self, work_item_id: str, session_id: str,
    ) -> bool:
        """Link a pre-existing replacement checkpoint to its one-shot allowance."""
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            recovery = connection.execute(
                """SELECT old_session_id,replacement_allowed,replacement_reserved,
                          replacement_session_id
                   FROM direct_execution_recoveries WHERE work_item_id=?""",
                (work_item_id,),
            ).fetchone()
            if recovery is None or recovery["old_session_id"] == session_id:
                return False
            if recovery["replacement_session_id"] == session_id:
                return False
            if recovery["replacement_reserved"] or recovery["replacement_session_id"]:
                raise McpError(
                    "replacement checkpoint conflicts with persisted replacement session"
                )
            if not recovery["replacement_allowed"]:
                raise McpError("replacement session is not allowed")
            changed = connection.execute(
                """UPDATE direct_execution_recoveries
                   SET replacement_reserved=1,replacement_session_id=?,updated_at=?
                   WHERE work_item_id=? AND replacement_allowed=1
                     AND replacement_reserved=0 AND replacement_session_id IS NULL""",
                (session_id, now, work_item_id),
            )
            if changed.rowcount != 1:
                raise McpError("replacement checkpoint adoption changed")
            checkpoint = connection.execute(
                """UPDATE direct_execution_sessions SET replacement_created=1,
                          updated_at=?
                   WHERE work_item_id=? AND session_id=?""",
                (now, work_item_id, session_id),
            )
            if checkpoint.rowcount != 1:
                raise McpError("replacement checkpoint disappeared during adoption")
            connection.execute(
                """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                       payload_json,occurred_at) VALUES(
                       'work_item',?,'replacement_checkpoint_adopted',?,?)""",
                (work_item_id, json.dumps({
                    "old_session_id": recovery["old_session_id"],
                    "replacement_session_id": session_id,
                    "replacement_sessions_allowed": 1,
                }, sort_keys=True), now),
            )
        return True

    def _finished(
        self,
        client: McpClient,
        request: dict[str, Any],
        session_id: str,
        metrics: dict[str, Any],
        polls: int,
        plan: ExecutionPlan,
    ) -> dict[str, Any]:
        work_item_id = str(request["work_item_id"])
        raw = {
            "session_id": session_id, "status": "finished", "metrics": metrics,
        }
        dashboard_url = self._dashboard_url(plan, session_id)
        result = {
            "work_item_id": work_item_id,
            "outcome": "finished",
            "evidence": {"run": {
                "id": f"{work_item_id}:{session_id}",
                "session_id": session_id, "status": "finished",
                "dashboard_url": dashboard_url,
                "metrics": metrics, "raw_result": raw,
            }},
        }
        violations = execution_result_violations({
            "schema_version": 1, "transport": "jesse_mcp",
            "work_item_id": work_item_id, "status": "finished",
            "session_id": session_id, "dashboard_url": dashboard_url,
            "metrics": metrics, "error": None, "raw_result": raw,
        })
        if violations:
            return self._record_and_return(
                client, work_item_id, polls, "blocked",
                blocker_code="invalid_execution_result",
                detail=(
                    "jesse-execution-result schema violation: "
                    + "; ".join(violations)
                )[:1000],
            )
        self._telemetry(client, work_item_id, "finished", polls, result)
        return result

    def _dashboard_url(self, plan: ExecutionPlan, session_id: str) -> str:
        base = self.config.dashboard_display_base_url.rstrip("/")
        if (
            plan.dashboard_path
            and plan.dashboard_path != "backtest"
            and base.endswith("/backtest")
        ):
            base = base[: -len("/backtest")] + f"/{plan.dashboard_path}"
        return f"{base}/{session_id}"

    def _infrastructure_failure_streak(self, work_item_id: str) -> int:
        """Count trailing consecutive retry outcomes in executor telemetry."""
        rows = self.database.rows(
            """SELECT outcome FROM direct_execution_telemetry
               WHERE work_item_id=? ORDER BY id DESC LIMIT ?""",
            (
                work_item_id,
                self.resource_policy.executor_infrastructure_failure_limit,
            ),
        )
        streak = 0
        for row in rows:
            if row["outcome"] != "retry":
                break
            streak += 1
        return streak

    def _record_and_return(
        self,
        client: McpClient,
        work_item_id: str,
        polls: int,
        outcome: str,
        *,
        blocker_code: str,
        detail: str,
        attempt_charged: bool | None = None,
    ) -> dict[str, Any]:
        if outcome == "retry":
            failures = self._infrastructure_failure_streak(work_item_id) + 1
            if failures >= self.resource_policy.executor_infrastructure_failure_limit:
                return self._record_and_return(
                    client, work_item_id, polls, "blocked",
                    blocker_code="infrastructure_circuit_broken",
                    detail=(
                        f"{failures} consecutive uncharged infrastructure "
                        "failures; circuit opened for this work item and must "
                        "be resolved through the blocker flow"
                    ),
                    attempt_charged=True,
                )
        result = {
            "work_item_id": work_item_id, "outcome": outcome,
            "blocker_code": blocker_code, "detail": detail,
        }
        if attempt_charged is not None:
            result["attempt_charged"] = attempt_charged
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
            _json_safe(result), separators=(",", ":"), sort_keys=True,
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

    def _record_orphaned_draft(
        self, work_item_id: str, experiment_id: str, session_id: str,
    ) -> None:
        """Audit a Jesse draft whose local checkpoint failed to persist.

        The Jesse tool surface exposes no draft-cancel operation, so the
        orphaned session is recorded for reconciliation and the next bounded
        dispatch creates a fresh draft.
        """
        try:
            with self.database.connect() as connection:
                connection.execute(
                    """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                           payload_json,occurred_at) VALUES(
                           'work_item',?,'direct_execution_draft_orphaned',?,?)""",
                    (work_item_id, json.dumps({
                        "experiment_id": experiment_id,
                        "session_id": session_id,
                    }, sort_keys=True), utc_now()),
                )
        except Exception:
            pass

    @staticmethod
    def _is_missing_session_error(error: BaseException) -> bool:
        detail = str(error).casefold()
        return all(marker in detail for marker in MISSING_SESSION_MARKERS)

    def _recover_missing_session_checkpoint(
        self,
        work_item_id: str,
        experiment_id: str,
        checkpoint: dict[str, Any],
        error: BaseException,
    ) -> str:
        """Invalidate one Jesse session that the server explicitly forgot.

        A Jesse restart can erase in-memory sessions while ATS still has a
        durable checkpoint. Grant exactly one replacement, only without a
        durable run, and make the mutation atomic so concurrent workers cannot
        create multiple replacement drafts.
        """
        if not self._is_missing_session_error(error):
            return "not_missing"
        session_id = str(checkpoint["session_id"])
        now = utc_now()
        reason = "Jesse reported persisted session not found"
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """SELECT session_id,state FROM direct_execution_sessions
                   WHERE work_item_id=?""",
                (work_item_id,),
            ).fetchone()
            if current is None or current["session_id"] != session_id:
                return "checkpoint_changed"
            if connection.execute(
                "SELECT 1 FROM runs WHERE work_item_id=? LIMIT 1",
                (work_item_id,),
            ).fetchone() is not None:
                return "durable_run_exists"
            recovery = connection.execute(
                """SELECT old_session_id,replacement_session_id
                   FROM direct_execution_recoveries WHERE work_item_id=?""",
                (work_item_id,),
            ).fetchone()
            if recovery is not None:
                if (
                    recovery["old_session_id"] == session_id
                    and recovery["replacement_session_id"] is None
                ):
                    connection.execute(
                        "DELETE FROM direct_execution_sessions WHERE work_item_id=?",
                        (work_item_id,),
                    )
                    return "already_registered"
                return "replacement_exhausted"
            connection.execute(
                """INSERT INTO direct_execution_recoveries(
                       work_item_id,old_session_id,old_state,reason,
                       replacement_allowed,created_at,updated_at
                   ) VALUES (?,?,?,?,1,?,?)""",
                (
                    work_item_id, session_id, current["state"], reason, now, now,
                ),
            )
            deleted = connection.execute(
                """DELETE FROM direct_execution_sessions
                   WHERE work_item_id=? AND session_id=?""",
                (work_item_id, session_id),
            )
            if deleted.rowcount != 1:
                raise McpError(
                    "missing Jesse session checkpoint changed during recovery"
                )
            connection.execute(
                """INSERT INTO events(aggregate_type,aggregate_id,event_type,
                       payload_json,occurred_at) VALUES(
                       'work_item',?,'missing_execution_session_recovered',?,?)""",
                (work_item_id, json.dumps({
                    "experiment_id": experiment_id,
                    "old_session_id": session_id,
                    "old_state": current["state"],
                    "reason": reason,
                    "replacement_allowance": 1,
                }, sort_keys=True), now),
            )
        return "recovered"

    def _checkpoint(self, work_item_id: str) -> dict[str, Any] | None:
        rows = self.database.rows(
            "SELECT * FROM direct_execution_sessions WHERE work_item_id=?",
            (work_item_id,),
        )
        return rows[0] if rows else None

    @staticmethod
    def _timestamp_seconds(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            numeric = float(value)
            return numeric / 1000 if numeric > 10_000_000_000 else numeric
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                return None
        return None

    def _observe_session(
        self,
        work_item_id: str,
        experiment_id: str,
        session_id: str,
        fingerprint: str,
        session: dict[str, Any],
    ) -> SessionClassification:
        previous = self._checkpoint(work_item_id) or {}
        results = _results(session)
        progress = _progress(results) if results is not None else None
        updated = session.get("updated_at") or session.get("updatedAt")
        updated_text = str(updated) if updated is not None else None
        same = (
            previous.get("last_jesse_updated_at") == updated_text
            and previous.get("last_progress") == progress
        )
        unchanged = (
            int(previous.get("unchanged_observations") or 0) + 1
            if same else 1
        )
        observed_at = utc_now()
        updated_seconds = self._timestamp_seconds(updated)
        if updated_seconds is None:
            updated_seconds = self._timestamp_seconds(
                previous.get("first_observed_at") or observed_at
            )
        stale_for = max(
            0.0,
            datetime.now(timezone.utc).timestamp() - (updated_seconds or 0.0),
        )
        classification = classify_jesse_session(
            session,
            unchanged_observations=unchanged,
            stale_for_seconds=stale_for,
            grace_seconds=self.config.zombie_grace_seconds,
            required_unchanged_observations=(
                self.config.zombie_unchanged_observations
            ),
        )
        with self.database.connect() as connection:
            connection.execute(
                """INSERT INTO direct_execution_sessions(
                       work_item_id,experiment_id,session_id,request_fingerprint,
                       state,first_observed_at,last_observed_at,
                       last_jesse_updated_at,last_progress,unchanged_observations,
                       created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(work_item_id) DO UPDATE SET
                       state=excluded.state,
                       first_observed_at=COALESCE(
                           direct_execution_sessions.first_observed_at,
                           excluded.first_observed_at
                       ),
                       last_observed_at=excluded.last_observed_at,
                       last_jesse_updated_at=excluded.last_jesse_updated_at,
                       last_progress=excluded.last_progress,
                       unchanged_observations=excluded.unchanged_observations,
                       updated_at=excluded.updated_at""",
                (
                    work_item_id, experiment_id, session_id, fingerprint,
                    classification.state, observed_at, observed_at, updated_text,
                    progress, unchanged, observed_at, observed_at,
                ),
            )
        return classification

    def _mark_recovery_attempted(self, work_item_id: str) -> None:
        with self.database.connect() as connection:
            changed = connection.execute(
                """UPDATE direct_execution_sessions SET recovery_attempted=1,
                          updated_at=?
                   WHERE work_item_id=? AND recovery_attempted=0""",
                (utc_now(), work_item_id),
            )
            if changed.rowcount != 1:
                raise McpError("zombie reconciliation already attempted")

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
                    json.dumps(_json_safe(metrics), separators=(",", ":"), sort_keys=True)
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

    @classmethod
    def _fetch_session(
        cls, client: McpClient, plan: ExecutionPlan, session_id: str,
    ) -> dict[str, Any]:
        """Fetch a session and normalize operation-specific shapes."""
        session = cls._session(client.call_tool(
            plan.get_tool, {"session_id": session_id},
        ))
        if plan.operation == "significance":
            return cls._normalize_significance_session(session)
        return session

    @staticmethod
    def _normalize_significance_session(session: dict[str, Any]) -> dict[str, Any]:
        """Map a significance-test session to the classifier's backtest shape.

        Jesse significance sessions expose terminal metrics at the top-level
        ``results`` key (observed_mean, annualized_return, p_value,
        n_simulations, n_observations) and store ``state`` as a config form with
        no ``results.executing`` flag and no ``metrics`` key. The shared
        classifier expects backtest shape (``state.results.executing`` for
        liveness, ``metrics`` for terminal output), so normalize before
        classification. Status transitions running -> finished are authoritative
        for liveness; progress is published over Redis and is not part of the
        session object, so it is left unset (None).
        """
        status = str(session.get("status") or "unknown").lower()
        raw_results = session.get("results")
        metrics = raw_results if isinstance(raw_results, dict) else None
        executing = (
            status in {"running", "pending", "queued", "starting", "started"}
        )
        normalized: dict[str, Any] = {
            "id": session.get("id") or session.get("session_id"),
            "status": status,
            "state": {"results": {"executing": executing}},
            "metrics": metrics,
            "updated_at": session.get("updated_at") or session.get("updatedAt"),
        }
        for key in ("exception", "error", "traceback"):
            if session.get(key):
                normalized[key] = session[key]
        return normalized

    @staticmethod
    def _operation(request: dict[str, Any]) -> str | None:
        operation = request.get("work_item", {}).get("operation")
        if operation is None:
            operation = OPERATION_BY_EXPERIMENT_TYPE.get(
                request.get("experiment", {}).get("experiment_type")
            )
        return operation

    @staticmethod
    def _mechanical_backtest(request: dict[str, Any]) -> bool:
        operation = DirectMcpDispatcher._operation(request)
        if operation not in {"backtest", "significance"}:
            return False
        if request.get("execution_context", {}).get("optimizer_parameters"):
            return False
        experiment = request.get("experiment", {})
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
    def _significance_metrics_complete(metrics: dict[str, Any]) -> bool:
        return all(
            field in metrics and metrics[field] is not None
            for field in SIGNIFICANCE_METRIC_FIELDS
        )

    @staticmethod
    def _requires_preparation(request: dict[str, Any]) -> bool:
        experiment = request.get("experiment", {})
        work = request.get("work_item", {})
        work_entry = work.get("entry_rule")
        experiment_entry = experiment.get("entry_rule")
        entry_rule = (
            work_entry if isinstance(work_entry, dict)
            else experiment_entry if isinstance(experiment_entry, dict)
            else {}
        )
        scope = (
            work.get("change_scope")
            or experiment.get("change_scope")
            or entry_rule.get("change_scope")
        )
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
        experiment = request.get("experiment", {})
        work_item = request.get("work_item", {})
        material = {
            "experiment_id": request.get("experiment_id"),
            "strategy_name": experiment.get("strategy_name"),
            "routes": experiment.get("routes"),
            "session_exchange_config": DirectMcpDispatcher._session_exchange_config(
                experiment,
            ),
            "operation": work_item.get("operation"),
            "execution_context": request.get("execution_context"),
        }
        if work_item.get("operation") == "significance":
            material["significance_parameters"] = work_item.get("parameters")
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
