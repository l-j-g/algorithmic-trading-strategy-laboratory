"""Typed workflow contracts. Values remain JSON and SQLite compatible."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class WorkState(StrEnum):
    SCHEDULED = "scheduled"
    READY = "ready"
    RUNNING = "running"
    WAITING_RETRY = "waiting_retry"
    BLOCKED = "blocked"
    FINISHED = "finished"
    ARCHIVED = "archived"


class Verdict(StrEnum):
    REJECT = "reject"
    REVISE = "revise"
    HPO_CANDIDATE = "hpo_candidate"
    PAPER_TRADE_CANDIDATE = "paper_trade_candidate"
    INCONCLUSIVE = "inconclusive"
    INFRASTRUCTURE_FAILURE = "infrastructure_failure"
    PASS = "pass"


class ExperimentType(StrEnum):
    BASELINE = "baseline"
    MULTI_WINDOW = "multi_window"
    COST_SENSITIVITY = "cost_sensitivity"
    OUT_OF_SAMPLE = "out_of_sample"
    SIGNIFICANCE = "significance"
    MONTE_CARLO = "monte_carlo"
    HPO = "hpo"
    HARNESS_CHECK = "harness_check"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    FINISHED = "finished"
    STOPPED = "stopped"
    TERMINATED = "terminated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RouteSpec:
    exchange: str
    symbol: str
    timeframe: str
    start_date: str
    finish_date: str


@dataclass(frozen=True)
class GateSpec:
    name: str
    operator: str
    threshold: float | str | None = None
    required: bool = True


@dataclass(frozen=True)
class ExperimentSpec:
    id: str
    strategy_name: str
    experiment_type: ExperimentType = ExperimentType.UNKNOWN
    hypothesis: str = ""
    archetype: str = ""
    target_regime: str = ""
    failure_regime: str = ""
    routes: tuple[RouteSpec, ...] = ()
    leverage: float | None = None
    fee_rate: float | None = None
    sizing_model: str = ""
    success_gates: tuple[GateSpec, ...] = ()
    failure_gates: tuple[GateSpec, ...] = ()
    parent_experiment_id: str | None = None
    source_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkItem:
    id: str
    experiment_id: str
    priority: int
    state: WorkState
    dependencies: tuple[str, ...] = ()
    attempts: int = 0
    retry_after: str | None = None
    blocker_code: str | None = None
    blocker_detail: str | None = None
    specification: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    id: str
    experiment_id: str
    work_item_id: str
    session_id: str
    status: RunStatus
    route: RouteSpec | dict[str, Any] | None = None
    dashboard_url: str | None = None
    metrics: dict[str, Any] | None = None
    raw_result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass(frozen=True)
class Evaluation:
    experiment_id: str
    verdict: Verdict
    summary: str = ""
    metrics_summary: str = ""
    next_step: str = ""
    evaluator: str = "legacy-import"
    evaluated_at: str = field(default_factory=utc_now)
