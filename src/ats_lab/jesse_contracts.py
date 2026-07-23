"""Typed boundary between the laboratory and a Jesse MCP execution worker.

This module describes data only. It deliberately contains no Jesse imports,
filesystem access, subprocess calls, or direct trading-system operations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from .models import GateSpec, RouteSpec, RunStatus


class JesseOperation(StrEnum):
    BACKTEST = "backtest"
    HPO = "hpo"
    SIGNIFICANCE = "significance"
    MONTE_CARLO = "monte_carlo"


@dataclass(frozen=True)
class JesseExecutionRequest:
    """Complete, framework-facing input produced from one claimed work item."""

    request_id: str
    experiment_id: str
    work_item_id: str
    operation: JesseOperation
    strategy_name: str
    routes: tuple[RouteSpec, ...]
    parameters: dict[str, Any] = field(default_factory=dict)
    success_gates: tuple[GateSpec, ...] = ()
    failure_gates: tuple[GateSpec, ...] = ()
    schema_version: int = 1
    transport: str = "jesse_mcp"

    def __post_init__(self) -> None:
        for name in ("request_id", "experiment_id", "work_item_id", "strategy_name"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if self.transport != "jesse_mcp":
            raise ValueError("Jesse execution transport must be jesse_mcp")
        if not self.routes:
            raise ValueError("at least one route is required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class JesseExecutionError:
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JesseExecutionResult:
    """Normalized evidence returned by a Jesse MCP execution worker."""

    request_id: str
    experiment_id: str
    work_item_id: str
    status: RunStatus
    session_id: str | None = None
    dashboard_url: str | None = None
    metrics: dict[str, Any] = field(default_factory=dict)
    error: JesseExecutionError | None = None
    schema_version: int = 1
    transport: str = "jesse_mcp"

    def __post_init__(self) -> None:
        for name in ("request_id", "experiment_id", "work_item_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")
        if self.schema_version != 1:
            raise ValueError("schema_version must be 1")
        if self.transport != "jesse_mcp":
            raise ValueError("Jesse execution transport must be jesse_mcp")
        if self.status in (RunStatus.DRAFT, RunStatus.RUNNING, RunStatus.FINISHED) and not self.session_id:
            raise ValueError(f"session_id is required for {self.status.value} results")
        if self.status == RunStatus.FINISHED and self.error is not None:
            raise ValueError("finished result cannot contain an error")
        if self.status in (RunStatus.STOPPED, RunStatus.TERMINATED) and self.error is None:
            raise ValueError(f"error is required for {self.status.value} results")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def jesse_request_from_payload(payload: dict[str, Any]) -> JesseExecutionRequest:
    routes = tuple(RouteSpec(**item) for item in payload.get("routes", ()))
    success_gates = tuple(GateSpec(**item) for item in payload.get("success_gates", ()))
    failure_gates = tuple(GateSpec(**item) for item in payload.get("failure_gates", ()))
    return JesseExecutionRequest(
        schema_version=int(payload.get("schema_version", 1)),
        transport=str(payload.get("transport", "jesse_mcp")),
        request_id=str(payload.get("request_id", "")),
        experiment_id=str(payload.get("experiment_id", "")),
        work_item_id=str(payload.get("work_item_id", "")),
        operation=JesseOperation(payload["operation"]),
        strategy_name=str(payload.get("strategy_name", "")),
        routes=routes,
        parameters=dict(payload.get("parameters", {})),
        success_gates=success_gates,
        failure_gates=failure_gates,
    )


def jesse_result_from_payload(payload: dict[str, Any]) -> JesseExecutionResult:
    error_payload = payload.get("error")
    error = JesseExecutionError(**error_payload) if error_payload is not None else None
    return JesseExecutionResult(
        schema_version=int(payload.get("schema_version", 1)),
        transport=str(payload.get("transport", "jesse_mcp")),
        request_id=str(payload.get("request_id", "")),
        experiment_id=str(payload.get("experiment_id", "")),
        work_item_id=str(payload.get("work_item_id", "")),
        status=RunStatus(payload.get("status", "unknown")),
        session_id=payload.get("session_id"),
        dashboard_url=payload.get("dashboard_url"),
        metrics=dict(payload.get("metrics", {})),
        error=error,
    )
