"""Deterministic job synthesis with entry-rule significance gating."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import load_json
from .database import WorkflowDatabase
from .models import ExperimentSpec, ExperimentType, GateSpec, RouteSpec, WorkItem, WorkState


ENTRY_CHANGE_SCOPES = {"new_entry", "entry_changed"}
NON_ENTRY_CHANGE_SCOPES = {"exit_only", "sizing_only", "risk_only", "refactor"}
SYNTHESIS_ACTIONS = {"new", "revise"}


@dataclass(frozen=True)
class SynthesisRequest:
    strategy_name: str
    hypothesis: str
    entry_rule: str
    change_scope: str
    routes: tuple[RouteSpec, ...]
    parent_entry_fingerprint: str | None = None
    priority: int = 100
    n_simulations: int = 2000
    random_seed: int | None = None
    action: str = "new"
    source_experiment_id: str | None = None
    controlled_change: str = ""
    lane: str | None = None
    archetype: str = ""
    target_regime: str = ""
    failure_regime: str = ""
    edge_thesis: str = ""
    cohort_id: str | None = None
    cohort_slot: int | None = None

    @property
    def entry_fingerprint(self) -> str:
        normalized = " ".join(self.entry_rule.split()).casefold()
        return hashlib.sha256(normalized.encode()).hexdigest()

    @property
    def job_fingerprint(self) -> str:
        if self.action == "new" and self.change_scope in ENTRY_CHANGE_SCOPES:
            return self.entry_fingerprint
        material = "|".join((
            self.entry_fingerprint, self.action, self.source_experiment_id or "",
            self.change_scope, " ".join(self.controlled_change.split()).casefold(),
            " ".join(self.hypothesis.split()).casefold(),
        ))
        return hashlib.sha256(material.encode()).hexdigest()


def synthesis_request_from_file(path: Path) -> SynthesisRequest:
    return synthesis_request_from_payload(load_json(path))


def synthesis_request_from_payload(payload: dict[str, Any]) -> SynthesisRequest:
    if payload.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    required = ("strategy_name", "hypothesis", "entry_rule", "change_scope", "routes")
    missing = [name for name in required if not payload.get(name)]
    if missing:
        raise ValueError("missing required fields: " + ", ".join(missing))
    scope = str(payload["change_scope"])
    if scope not in ENTRY_CHANGE_SCOPES | NON_ENTRY_CHANGE_SCOPES:
        raise ValueError(f"unsupported change_scope: {scope}")
    action = str(payload.get("action", "new"))
    if action not in SYNTHESIS_ACTIONS:
        raise ValueError(f"unsupported synthesis action: {action}")
    if action == "revise" and not payload.get("source_experiment_id"):
        raise ValueError("revise action requires source_experiment_id")
    if action == "revise" and not payload.get("controlled_change"):
        raise ValueError("revise action requires one controlled_change")
    routes = tuple(RouteSpec(**route) for route in payload["routes"])
    if not routes:
        raise ValueError("at least one route is required")
    n_simulations = int(payload.get("n_simulations", 2000))
    if n_simulations < 2000:
        raise ValueError("n_simulations must be at least 2000")
    return SynthesisRequest(
        strategy_name=str(payload["strategy_name"]), hypothesis=str(payload["hypothesis"]),
        entry_rule=str(payload["entry_rule"]), change_scope=scope, routes=routes,
        parent_entry_fingerprint=payload.get("parent_entry_fingerprint"),
        priority=int(payload.get("priority", 100)), n_simulations=n_simulations,
        random_seed=payload.get("random_seed"),
        action=action, source_experiment_id=payload.get("source_experiment_id"),
        controlled_change=str(payload.get("controlled_change", "")),
        lane=payload.get("lane"), archetype=str(payload.get("archetype", "")),
        target_regime=str(payload.get("target_regime", "")),
        failure_regime=str(payload.get("failure_regime", "")),
        edge_thesis=str(payload.get("edge_thesis", "")),
        cohort_id=payload.get("cohort_id"),
        cohort_slot=(int(payload["cohort_slot"]) if payload.get("cohort_slot") is not None else None),
    )


def _slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", value.upper()).strip("-")[:40] or "STRATEGY"


def _latest_p_value(database: WorkflowDatabase, fingerprint: str) -> float | None:
    rows = database.rows(
        """SELECT json_extract(r.metrics_json, '$.p_value') AS p_value
           FROM runs r JOIN experiments e ON e.id=r.experiment_id
           WHERE e.experiment_type='significance' AND r.status='finished'
             AND json_extract(e.specification_json, '$.entry_rule.fingerprint')=?
             AND json_extract(r.metrics_json, '$.p_value') IS NOT NULL
           ORDER BY COALESCE(r.finished_at, r.started_at) DESC, r.id DESC LIMIT 1""",
        (fingerprint,),
    )
    return float(rows[0]["p_value"]) if rows else None


def _ensure_experiment(database: WorkflowDatabase, spec: ExperimentSpec) -> None:
    if not database.rows("SELECT id FROM experiments WHERE id=?", (spec.id,)):
        database.upsert_experiment(spec)


def _ensure_work_item(database: WorkflowDatabase, item: WorkItem) -> None:
    if not database.rows("SELECT id FROM work_items WHERE id=?", (item.id,)):
        database.upsert_work_item(item)


def _set_state(database: WorkflowDatabase, work_item_id: str, state: WorkState) -> None:
    with database.connect() as connection:
        connection.execute(
            """UPDATE work_items SET state=?, blocker_code=NULL, blocker_detail=NULL,
               claimed_by=NULL, claimed_at=NULL WHERE id=? AND state IN ('scheduled','ready')""",
            (state.value, work_item_id),
        )


def synthesize(
    database: WorkflowDatabase, request: SynthesisRequest, *, source_path: str = "",
    release_ready: bool = True,
) -> dict[str, Any]:
    """Create or reconcile significance and baseline jobs for one research idea."""
    database.initialize()
    fingerprint = request.entry_fingerprint
    stem = f"{_slug(request.strategy_name)}-{request.job_fingerprint[:8].upper()}"
    significance_id = f"{stem}-SIG"
    baseline_id = f"{stem}-BL"
    lineage = {
        "fingerprint": fingerprint,
        "description": request.entry_rule,
        "parent_fingerprint": request.parent_entry_fingerprint,
        "change_scope": request.change_scope,
        "action": request.action,
        "source_experiment_id": request.source_experiment_id,
        "controlled_change": request.controlled_change,
        "lane": request.lane,
        "archetype": request.archetype,
        "target_regime": request.target_regime,
        "failure_regime": request.failure_regime,
        "edge_thesis": request.edge_thesis,
        "cohort_id": request.cohort_id,
        "cohort_slot": request.cohort_slot,
    }
    p_value = _latest_p_value(database, fingerprint)
    needs_significance = request.change_scope in ENTRY_CHANGE_SCOPES

    if needs_significance:
        sig_parameters: dict[str, Any] = {"n_simulations": request.n_simulations}
        if request.random_seed is not None:
            sig_parameters["random_seed"] = request.random_seed
        sig_spec = ExperimentSpec(
            id=significance_id, strategy_name=request.strategy_name,
            experiment_type=ExperimentType.SIGNIFICANCE, hypothesis=request.hypothesis,
            archetype=request.archetype, target_regime=request.target_regime,
            failure_regime=request.failure_regime,
            routes=(request.routes[0],),
            success_gates=(GateSpec("p_value", "<", 0.05),),
            failure_gates=(GateSpec("p_value", ">", 0.10),),
            parent_experiment_id=request.source_experiment_id, source_path=source_path,
        )
        _ensure_experiment(database, sig_spec)
        _merge_experiment_metadata(database, significance_id, lineage, {"operation": "significance", "parameters": sig_parameters})
        sig_state = WorkState.FINISHED if p_value is not None else (WorkState.READY if release_ready else WorkState.SCHEDULED)
        _ensure_work_item(database, WorkItem(
            id=significance_id, experiment_id=significance_id, priority=request.priority,
            state=sig_state, specification={"operation": "significance", "parameters": sig_parameters,
                                             "entry_rule": lineage},
        ))

    baseline_state = WorkState.READY if release_ready else WorkState.SCHEDULED
    decision = "significance_not_required"
    dependencies: tuple[str, ...] = ()
    if needs_significance:
        dependencies = (significance_id,)
        if p_value is None:
            baseline_state, decision = WorkState.SCHEDULED, "awaiting_significance"
        elif p_value < 0.05:
            baseline_state = WorkState.READY if release_ready else WorkState.SCHEDULED
            decision = "significance_passed" if release_ready else "significance_passed_capacity_held"
        elif p_value <= 0.10:
            baseline_state, decision = WorkState.SCHEDULED, "significance_inconclusive"
        else:
            baseline_state, decision = WorkState.ARCHIVED, "significance_failed"

    baseline_spec = ExperimentSpec(
        id=baseline_id, strategy_name=request.strategy_name, experiment_type=ExperimentType.BASELINE,
        hypothesis=request.hypothesis, archetype=request.archetype,
        target_regime=request.target_regime, failure_regime=request.failure_regime,
        routes=request.routes,
        parent_experiment_id=significance_id if needs_significance else request.source_experiment_id,
        source_path=source_path,
    )
    _ensure_experiment(database, baseline_spec)
    _merge_experiment_metadata(database, baseline_id, lineage, {"operation": "backtest"})
    _ensure_work_item(database, WorkItem(
        id=baseline_id, experiment_id=baseline_id, priority=request.priority + (1 if needs_significance else 0),
        state=baseline_state, dependencies=dependencies,
        specification={"operation": "backtest", "entry_rule": lineage, "gate_decision": decision},
    ))
    _set_state(database, baseline_id, baseline_state)
    if p_value is not None:
        _set_state(database, significance_id, WorkState.FINISHED)
    return {
        "entry_fingerprint": fingerprint, "change_scope": request.change_scope,
        "job_fingerprint": request.job_fingerprint,
        "significance_job": significance_id if needs_significance else None,
        "baseline_job": baseline_id, "baseline_state": baseline_state.value,
        "decision": decision, "p_value": p_value,
        "released_ready": release_ready,
    }


def _merge_experiment_metadata(database: WorkflowDatabase, experiment_id: str, entry_rule: dict[str, Any], extra: dict[str, Any]) -> None:
    with database.connect() as connection:
        row = connection.execute("SELECT specification_json FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        payload = json.loads(row["specification_json"])
        payload["entry_rule"] = entry_rule
        payload.update(extra)
        connection.execute("UPDATE experiments SET specification_json=? WHERE id=?", (json.dumps(payload, sort_keys=True), experiment_id))
