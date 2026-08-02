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
from datetime import datetime, timezone
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
            if checkpoint is not None:
                self._adopt_replacement_checkpoint(
                    work_item_id, str(checkpoint["session_id"]),
                )
            if checkpoint is None:
                session_id, replacement, created_now = self._create_or_resume_session(
                    client, request,
                )
                self._save_checkpoint(
                    work_item_id, experiment_id, session_id, fingerprint, "draft",
                )
                if replacement:
                    with self.database.connect() as connection:
                        connection.execute(
                            """UPDATE direct_execution_sessions
                               SET replacement_created=1 WHERE work_item_id=?""",
                            (work_item_id,),
                        )
                if created_now:
                    session = self._start_and_verify(client, session_id)
                else:
                    session = self._session(client.call_tool(
                        "get_backtest_session", {"session_id": session_id},
                    ))
                    if self._status(session) == "draft":
                        session = self._start_and_verify(client, session_id)
            else:
                session_id = checkpoint["session_id"]
                if checkpoint["state"] in {"finished", "terminal_success"}:
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
            classification = self._observe_session(
                work_item_id, experiment_id, session_id, fingerprint, session,
            )
            delay = self.config.poll_initial_seconds
            for polls in range(1, self.config.max_polls + 1):
                if polls > 1 or classification.state not in {
                    "terminal_success", "terminal_failure",
                }:
                    session = self._session(client.call_tool(
                        "get_backtest_session", {"session_id": session_id},
                    ))
                    classification = self._observe_session(
                        work_item_id, experiment_id, session_id, fingerprint,
                        session,
                    )
                if classification.state == "terminal_success":
                    metrics = _metrics(session, _results(session))
                    if not isinstance(metrics, dict):
                        return self._record_and_return(
                            client, work_item_id, polls, "retry",
                            blocker_code="invalid_jesse_metrics",
                            detail="terminal Jesse session metrics must be object",
                            attempt_charged=False,
                        )
                    self._save_checkpoint(
                        work_item_id, experiment_id, session_id, fingerprint,
                        "terminal_success", metrics=metrics,
                    )
                    return self._finished(
                        client, request, session_id, metrics, polls,
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
                    return self._record_and_return(
                        client, work_item_id, polls, "retry",
                        blocker_code="malformed_jesse_session",
                        detail="Jesse session response lacks required execution state",
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
                    client.call_tool("run_backtest", {"session_id": session_id})
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
                    client, work_item_id, polls, "retry",
                    blocker_code="jesse_zombie_recovery_required",
                    detail=(
                        f"session {session_id} remains non-executing after one "
                        "reconciliation"
                    ),
                    attempt_charged=False,
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

    def _create_or_resume_session(
        self, client: McpClient, request: dict[str, Any],
    ) -> tuple[str, bool, bool]:
        work_item_id = str(request["work_item_id"])
        rows = self.database.rows(
            """SELECT replacement_allowed,replacement_reserved,
                      replacement_session_id
               FROM direct_execution_recoveries WHERE work_item_id=?""",
            (work_item_id,),
        )
        if not rows:
            return self._create(client, request), False, True
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
        session_id = self._create(client, request)
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
        attempt_charged: bool | None = None,
    ) -> dict[str, Any]:
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
