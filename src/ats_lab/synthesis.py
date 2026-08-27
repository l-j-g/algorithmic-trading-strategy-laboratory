"""Deterministic job synthesis with entry-rule significance gating."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contracts import load_json
from .database import WorkflowDatabase
from .models import DataRouteSpec, ExperimentSpec, ExperimentType, GateSpec, RouteSpec, WorkItem, WorkState
from .strategy_dependencies import data_route_dicts, merge_data_routes


ENTRY_CHANGE_SCOPES = {"new_entry", "entry_changed"}
NON_ENTRY_CHANGE_SCOPES = {"exit_only", "sizing_only", "risk_only", "refactor"}
SYNTHESIS_ACTIONS = {"new", "revise"}
PROPOSAL_TYPES = {"new_concept", "controlled_improvement"}
TYPED_PROPOSAL_MARKERS = {
    "type", "thesis", "falsifiability_criteria", "entry_rule_summary",
    "why_this_now", "expected_edge_type",
}
TYPED_PROPOSAL_REQUIRED_FIELDS = {
    "type", "source_experiment_id", "controlled_change", "thesis", "archetype",
    "target_regime", "failure_regime", "falsifiability_criteria", "entry_rule_summary",
    "why_this_now", "expected_edge_type",
}


@dataclass(frozen=True)
class SynthesisRequest:
    strategy_name: str
    hypothesis: str
    entry_rule: str
    change_scope: str
    routes: tuple[RouteSpec, ...]
    data_routes: tuple[DataRouteSpec, ...] = ()
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
    proposal_type: str | None = None
    thesis: str = ""
    falsifiability_criteria: str = ""
    entry_rule_summary: str = ""
    why_this_now: str = ""
    expected_edge_type: str = ""
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
        ))
        return hashlib.sha256(material.encode()).hexdigest()


def synthesis_request_from_file(path: Path) -> SynthesisRequest:
    return synthesis_request_from_payload(load_json(path))


def synthesis_request_from_payload(payload: dict[str, Any]) -> SynthesisRequest:
    payload, proposal_type = _normalize_typed_proposal(payload)
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
    data_routes = tuple(
        DataRouteSpec(**route) for route in payload.get("data_routes", [])
    )
    n_simulations = int(payload.get("n_simulations", 2000))
    if n_simulations < 2000:
        raise ValueError("n_simulations must be at least 2000")
    return SynthesisRequest(
        strategy_name=str(payload["strategy_name"]), hypothesis=str(payload["hypothesis"]),
        entry_rule=str(payload["entry_rule"]), change_scope=scope, routes=routes,
        data_routes=data_routes,
        parent_entry_fingerprint=payload.get("parent_entry_fingerprint"),
        priority=int(payload.get("priority", 100)), n_simulations=n_simulations,
        random_seed=payload.get("random_seed"),
        action=action, source_experiment_id=payload.get("source_experiment_id"),
        controlled_change=str(payload.get("controlled_change", "")),
        lane=payload.get("lane"), archetype=str(payload.get("archetype", "")),
        target_regime=str(payload.get("target_regime", "")),
        failure_regime=str(payload.get("failure_regime", "")),
        edge_thesis=str(payload.get("edge_thesis") or payload.get("expected_edge_type", "")),
        proposal_type=proposal_type,
        thesis=str(payload.get("thesis") or payload["hypothesis"]),
        falsifiability_criteria=str(
            payload.get("falsifiability_criteria")
            or "Reject when the stated edge is absent in the target regime."
        ),
        entry_rule_summary=str(payload.get("entry_rule_summary") or payload["entry_rule"]),
        why_this_now=str(
            payload.get("why_this_now")
            or payload.get("controlled_change")
            or "Selected from the current evidence and regime coverage."
        ),
        expected_edge_type=str(
            payload.get("expected_edge_type")
            or payload.get("edge_thesis")
            or "unclassified"
        ),
        cohort_id=payload.get("cohort_id"),
        cohort_slot=(int(payload["cohort_slot"]) if payload.get("cohort_slot") is not None else None),
    )


def _normalize_typed_proposal(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Validate an agent proposal and map its typed fields to the legacy request.

    Legacy CLI/manual payloads intentionally remain supported. Presence of the new
    ``type`` marker, or any typed-only field, opts into strict validation so partial
    or ambiguous agent proposals cannot reach deterministic persistence.
    """
    if not isinstance(payload, dict):
        raise ValueError("synthesis request must be an object")
    if not TYPED_PROPOSAL_MARKERS.intersection(payload):
        return dict(payload), None

    missing = sorted(TYPED_PROPOSAL_REQUIRED_FIELDS - payload.keys())
    if missing:
        raise ValueError("typed proposal missing fields: " + ", ".join(missing))
    proposal_type = payload["type"]
    if proposal_type not in PROPOSAL_TYPES:
        raise ValueError(f"unsupported proposal type: {proposal_type}")

    normalized = dict(payload)
    for name in TYPED_PROPOSAL_REQUIRED_FIELDS - {"source_experiment_id", "controlled_change"}:
        value = payload[name]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"typed proposal field must be a non-empty string: {name}")
    source_experiment_id = payload["source_experiment_id"]
    controlled_change = payload["controlled_change"]
    if proposal_type == "new_concept":
        if source_experiment_id is not None:
            raise ValueError("new_concept source_experiment_id must be null")
        if not isinstance(controlled_change, str):
            raise ValueError("typed proposal field must be a string: controlled_change")
        expected_action, expected_lane = "new", "new_concept"
        normalized.setdefault("change_scope", "new_entry")
    else:
        if not isinstance(source_experiment_id, str) or not source_experiment_id.strip():
            raise ValueError("controlled_improvement source_experiment_id must be non-empty")
        if not isinstance(controlled_change, str) or not controlled_change.strip():
            raise ValueError("controlled_improvement controlled_change must be non-empty")
        expected_action, expected_lane = "revise", "improvement"
        if not payload.get("change_scope"):
            raise ValueError("controlled_improvement requires change_scope")

    if payload.get("action", expected_action) != expected_action:
        raise ValueError(f"{proposal_type} proposal requires action={expected_action}")
    if payload.get("lane", expected_lane) != expected_lane:
        raise ValueError(f"{proposal_type} proposal requires lane={expected_lane}")
    normalized["action"] = expected_action
    normalized["lane"] = expected_lane
    normalized.setdefault("hypothesis", payload["thesis"])
    normalized.setdefault("entry_rule", payload["entry_rule_summary"])
    normalized.setdefault("edge_thesis", payload["expected_edge_type"])
    return normalized, proposal_type


def _slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "-", value.upper()).strip("-")[:40] or "STRATEGY"


def benjamini_hochberg(p_values: list[float], level: float) -> list[dict[str, Any]]:
    """Rank one family of p-values with Benjamini-Hochberg critical values.

    Returns one finding per hypothesis in the input order: 1-based rank,
    ``rank * level / m`` critical threshold, and whether the step-up
    procedure rejects it (every p-value at or below the largest rank k
    satisfying p(k) <= k*level/m is rejected).
    """
    if not 0 < level <= 1:
        raise ValueError("FDR level must be in (0, 1]")
    total = len(p_values)
    order = sorted(range(total), key=lambda index: (p_values[index], index))
    ranks = {index: rank for rank, index in enumerate(order, start=1)}
    rejected_rank = 0
    for rank, index in enumerate(order, start=1):
        if p_values[index] <= rank * level / total:
            rejected_rank = rank
    return [
        {
            "rank": ranks[index],
            "threshold": ranks[index] * level / total,
            "rejected": ranks[index] <= rejected_rank,
        }
        for index in range(total)
    ]


def _binding_significance_test(database: WorkflowDatabase, fingerprint: str) -> dict[str, Any] | None:
    """Return the first finished significance test for a canonical fingerprint.

    First-test-wins: the earliest finished run is binding; later re-tests are
    stored and visible but never flip baseline readiness.
    """
    rows = database.rows(
        """SELECT r.id AS run_id,
                  json_extract(r.metrics_json, '$.p_value') AS p_value,
                  COALESCE(r.finished_at, r.started_at) AS decided_at
           FROM runs r JOIN experiments e ON e.id=r.experiment_id
           WHERE e.experiment_type='significance' AND r.status='finished'
             AND json_extract(e.specification_json, '$.entry_rule.fingerprint')=?
             AND json_extract(r.metrics_json, '$.p_value') IS NOT NULL
           ORDER BY COALESCE(r.finished_at, r.started_at) ASC, r.id ASC LIMIT 1""",
        (fingerprint,),
    )
    if not rows:
        return None
    return {
        "run_id": rows[0]["run_id"],
        "p_value": float(rows[0]["p_value"]),
        "decided_at": rows[0]["decided_at"],
    }


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


def _inherited_data_routes(
    database: WorkflowDatabase, source_experiment_id: str | None,
) -> tuple[DataRouteSpec, ...]:
    """Read auxiliary routes from a revision parent without guessing from prose."""
    if not source_experiment_id:
        return ()
    rows = database.rows(
        "SELECT specification_json FROM experiments WHERE id=?",
        (source_experiment_id,),
    )
    if not rows:
        return ()
    payload = json.loads(rows[0]["specification_json"] or "{}")
    routes = payload.get("data_routes", [])
    if routes is None:
        return ()
    if not isinstance(routes, list):
        raise ValueError(
            f"source experiment data_routes must be an array: {source_experiment_id}"
        )
    try:
        return tuple(DataRouteSpec(**route) for route in routes)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"source experiment data_routes are invalid: {source_experiment_id}"
        ) from error


def _effective_data_routes(
    database: WorkflowDatabase, request: SynthesisRequest,
) -> tuple[DataRouteSpec, ...]:
    """Union explicit, inherited, and reviewed strategy dependency routes."""
    primary_routes = [asdict(route) for route in request.routes]
    return merge_data_routes(
        request.strategy_name,
        primary_routes,
        _inherited_data_routes(database, request.source_experiment_id),
        request.data_routes,
    )


def synthesize(
    database: WorkflowDatabase, request: SynthesisRequest, *, source_path: str = "",
    release_ready: bool = True,
) -> dict[str, Any]:
    """Create or reconcile significance and baseline jobs for one research idea."""
    database.initialize()
    data_routes = _effective_data_routes(database, request)
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
        "thesis": request.thesis,
        "falsifiability_criteria": request.falsifiability_criteria,
        "entry_rule_summary": request.entry_rule_summary,
        "proposal_type": request.proposal_type,
        "why_this_now": request.why_this_now,
        "expected_edge_type": request.expected_edge_type,
        "cohort_id": request.cohort_id,
        "cohort_slot": request.cohort_slot,
    }
    binding = _binding_significance_test(database, fingerprint)
    p_value = binding["p_value"] if binding else None
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
            routes=request.routes,
            data_routes=data_routes,
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
                                             "entry_rule": lineage,
                                             "data_routes": [
                                                 route.__dict__
                                                 for route in data_routes
                                             ]},
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
        id=baseline_id, strategy_name=request.strategy_name,
        experiment_type=ExperimentType.BASELINE,
        hypothesis=request.hypothesis, archetype=request.archetype,
        target_regime=request.target_regime, failure_regime=request.failure_regime,
        routes=request.routes,
        data_routes=data_routes,
        parent_experiment_id=significance_id if needs_significance else request.source_experiment_id,
        source_path=source_path,
    )
    _ensure_experiment(database, baseline_spec)
    _merge_experiment_metadata(database, baseline_id, lineage, {"operation": "backtest"})
    _ensure_work_item(database, WorkItem(
        id=baseline_id, experiment_id=baseline_id, priority=request.priority + (1 if needs_significance else 0),
        state=baseline_state, dependencies=dependencies,
        specification={
            "operation": "backtest", "entry_rule": lineage,
            "gate_decision": decision,
            "data_routes": data_route_dicts(data_routes),
        },
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
        "binding_significance_test": binding,
        "released_ready": release_ready,
    }


def _merge_experiment_metadata(database: WorkflowDatabase, experiment_id: str, entry_rule: dict[str, Any], extra: dict[str, Any]) -> None:
    with database.connect() as connection:
        row = connection.execute("SELECT specification_json FROM experiments WHERE id=?", (experiment_id,)).fetchone()
        payload = json.loads(row["specification_json"])
        payload["entry_rule"] = entry_rule
        payload.update(extra)
        connection.execute("UPDATE experiments SET specification_json=? WHERE id=?", (json.dumps(payload, sort_keys=True), experiment_id))
