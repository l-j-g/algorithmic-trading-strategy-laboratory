"""Parsing and validation for versioned JSON CLI contracts."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import json

from .models import Evaluation, ExperimentSpec, ExperimentType, GateSpec, RouteSpec, Verdict, WorkItem, WorkState, utc_now


def _required(payload: dict[str, Any], *names: str) -> None:
    missing = [name for name in names if not payload.get(name)]
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))


def load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("contract root must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    return payload


def experiment_from_payload(payload: dict[str, Any], source_path: str = "") -> ExperimentSpec:
    _required(payload, "id", "strategy_name", "experiment_type")
    routes = tuple(RouteSpec(**route) for route in payload.get("routes", []))
    success_gates = tuple(GateSpec(**gate) for gate in payload.get("success_gates", []))
    failure_gates = tuple(GateSpec(**gate) for gate in payload.get("failure_gates", []))
    return ExperimentSpec(
        id=str(payload["id"]), strategy_name=str(payload["strategy_name"]),
        experiment_type=ExperimentType(payload["experiment_type"]),
        hypothesis=str(payload.get("hypothesis", "")), archetype=str(payload.get("archetype", "")),
        target_regime=str(payload.get("target_regime", "")), failure_regime=str(payload.get("failure_regime", "")),
        routes=routes, leverage=payload.get("leverage"), fee_rate=payload.get("fee_rate"),
        sizing_model=str(payload.get("sizing_model", "")), success_gates=success_gates,
        failure_gates=failure_gates, parent_experiment_id=payload.get("parent_experiment_id"), source_path=source_path,
    )


def work_item_from_payload(payload: dict[str, Any]) -> WorkItem:
    _required(payload, "id", "experiment_id")
    return WorkItem(
        id=str(payload["id"]), experiment_id=str(payload["experiment_id"]),
        priority=int(payload.get("priority", 100)), state=WorkState(payload.get("state", "scheduled")),
        dependencies=tuple(str(value) for value in payload.get("dependencies", [])),
        specification=dict(payload.get("specification", {})),
    )


def evaluation_from_payload(payload: dict[str, Any]) -> Evaluation:
    _required(payload, "experiment_id", "verdict")
    return Evaluation(
        experiment_id=str(payload["experiment_id"]), verdict=Verdict(str(payload["verdict"]).replace("-", "_")),
        summary=str(payload.get("summary", "")), metrics_summary=str(payload.get("metrics_summary", "")),
        next_step=str(payload.get("next_step", "")), evaluator=str(payload.get("evaluator", "ats-lab")),
        evaluated_at=str(payload.get("evaluated_at") or utc_now()),
    )
