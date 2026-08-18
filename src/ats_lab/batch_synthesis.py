"""Batch planning context and guarded persistence for idle synthesis."""
from __future__ import annotations

from typing import Any, Mapping

from .database import WorkflowDatabase
from .research_memory import ResearchMemoryAdapter, compact_advisory_memory
from .resources import ResourcePolicy
from .synthesis import SynthesisRequest, synthesis_request_from_payload, synthesize


INSPECT_LIMIT = 25
GENERATE_LIMIT = 25
ACTIVE_READY_LIMIT = 3
MAX_REVISION_DEPTH = 3
LOCKED_VERDICTS = {"hpo_candidate", "paper_trade_candidate"}
REVISION_VERDICTS = {"revise", "inconclusive"}
CONTEXT_EVIDENCE_LIMIT = 4
CONTEXT_FAILURE_LIMIT = 8
CONTEXT_FINGERPRINT_LIMIT = 12
CONTEXT_THEME_LIMIT = 12
TYPED_PROPOSAL_FIELDS = (
    "type", "source_experiment_id", "controlled_change", "thesis",
    "archetype", "target_regime", "failure_regime", "falsifiability_criteria",
    "entry_rule_summary", "why_this_now", "expected_edge_type",
)
TYPED_CONTRACT_MARKERS = frozenset({
    "type", "thesis", "falsifiability_criteria", "entry_rule_summary",
    "why_this_now", "expected_edge_type",
})


def revision_depth(database: WorkflowDatabase, experiment_id: str) -> int:
    rows = database.rows(
        """WITH RECURSIVE lineage(id, parent_id, depth) AS (
               SELECT id, parent_experiment_id, 0 FROM experiments WHERE id=?
               UNION ALL
               SELECT e.id, e.parent_experiment_id, lineage.depth + 1
               FROM experiments e JOIN lineage ON e.id=lineage.parent_id
               WHERE lineage.depth < 100
           ) SELECT MAX(depth) AS depth FROM lineage""",
        (experiment_id,),
    )
    return int(rows[0]["depth"] or 0) if rows else 0


def is_promotion_locked(database: WorkflowDatabase, experiment_id: str) -> bool:
    rows = database.rows(
        """SELECT 1 FROM evaluations WHERE experiment_id=?
           AND verdict IN ('hpo_candidate','paper_trade_candidate') LIMIT 1""",
        (experiment_id,),
    )
    return bool(rows)


def build_batch_context(
    database: WorkflowDatabase, limit: int | None = None,
    policy: ResourcePolicy | None = None,
    memory_adapter: ResearchMemoryAdapter | None = None,
) -> dict[str, Any]:
    """Build bounded synthesis context; Honcho recall remains advisory only."""
    policy = policy or ResourcePolicy()
    limit = limit or policy.synthesis_inspect_limit
    revision_limit = min(limit, policy.synthesis_max_improvements)
    evidence_limit = CONTEXT_EVIDENCE_LIMIT
    latest = database.rows(
        """WITH ranked AS (
               SELECT ev.*, ROW_NUMBER() OVER (
                   PARTITION BY ev.experiment_id ORDER BY ev.evaluated_at DESC, ev.id DESC
               ) AS rn
               FROM evaluations ev
           )
           SELECT e.id AS source_experiment_id, s.name AS strategy, e.hypothesis,
                  e.experiment_type, e.specification_json, ranked.verdict,
                  ranked.evaluated_at
           FROM ranked JOIN experiments e ON e.id=ranked.experiment_id
           LEFT JOIN strategies s ON s.id=e.strategy_id
           WHERE ranked.rn=1 AND ranked.verdict IN ('revise','inconclusive')
             AND NOT EXISTS (
                 SELECT 1 FROM evaluations locked WHERE locked.experiment_id=e.id
                 AND locked.verdict IN ('hpo_candidate','paper_trade_candidate')
             )
           ORDER BY CASE ranked.verdict WHEN 'revise' THEN 0 ELSE 1 END,
                    ranked.evaluated_at DESC LIMIT ?""",
        (revision_limit,),
    )
    revisions: list[dict[str, Any]] = []
    for row in latest:
        item = dict(row)
        item["revision_depth"] = revision_depth(database, row["source_experiment_id"])
        item["evidence"] = [
            evidence.to_compact_dict()
            for evidence in database.query_normalized_evidence(
                {"experiment_id": row["source_experiment_id"]},
                limit=evidence_limit,
            )
        ]
        if item["revision_depth"] < policy.synthesis_max_revision_depth:
            revisions.append(item)
    remaining = max(0, limit - len(revisions))
    revision_ids = {row["source_experiment_id"] for row in revisions}
    scheduled = database.rows(
        """SELECT w.id AS work_item_id, e.id AS source_experiment_id, s.name AS strategy,
                  w.priority, w.specification_json AS work_specification_json,
                  e.hypothesis, e.experiment_type, e.specification_json
           FROM work_items w JOIN experiments e ON e.id=w.experiment_id
           LEFT JOIN strategies s ON s.id=e.strategy_id
           WHERE w.state='scheduled'
             AND COALESCE(
               json_extract(w.specification_json,'$.readiness.status'),
               'ready'
             )!='requirements_pending'
             AND NOT EXISTS (
                 SELECT 1 FROM evaluations locked WHERE locked.experiment_id=e.id
                 AND locked.verdict IN ('hpo_candidate','paper_trade_candidate')
             )
           ORDER BY w.priority,w.created_at,w.id LIMIT ?""",
        (limit,),
    )
    scheduled = [row for row in scheduled if row["source_experiment_id"] not in revision_ids][:remaining]
    locked_count = database.rows(
        """SELECT COUNT(DISTINCT experiment_id) AS count FROM evaluations
           WHERE verdict IN ('hpo_candidate','paper_trade_candidate')"""
    )[0]["count"]
    learnings = database.rows(
        """WITH latest AS (
               SELECT ev.*, ROW_NUMBER() OVER (
                   PARTITION BY ev.experiment_id ORDER BY ev.evaluated_at DESC,ev.id DESC
               ) AS rn
               FROM evaluations ev
           )
           SELECT e.id AS experiment_id,s.name AS strategy,e.archetype,e.target_regime,
                  e.failure_regime,latest.verdict
           FROM latest JOIN experiments e ON e.id=latest.experiment_id
           LEFT JOIN strategies s ON s.id=e.strategy_id
           WHERE latest.rn=1
           ORDER BY latest.evaluated_at DESC LIMIT ?""",
        (limit + len(revision_ids),),
    )
    learning_limit = max(0, limit - len(revisions) - len(scheduled))
    learnings = [
        learning for learning in learnings
        if learning["experiment_id"] not in revision_ids
    ][:learning_limit]
    for learning in learnings:
        learning["evidence"] = [
            evidence.to_compact_dict()
            for evidence in database.query_normalized_evidence(
                {"experiment_id": learning["experiment_id"]},
                limit=evidence_limit,
            )
        ]
    recent = database.rows(
        """WITH ranked AS (
               SELECT ev.*, ROW_NUMBER() OVER (
                   PARTITION BY ev.experiment_id ORDER BY ev.evaluated_at DESC, ev.id DESC
               ) AS rn
               FROM evaluations ev
           )
           SELECT e.id AS experiment_id, s.name AS strategy, e.archetype,
                  e.target_regime, e.failure_regime, ranked.verdict,
                  ranked.summary, ranked.next_step, ranked.evaluated_at
           FROM ranked JOIN experiments e ON e.id=ranked.experiment_id
           LEFT JOIN strategies s ON s.id=e.strategy_id
           WHERE ranked.rn=1
             AND ranked.verdict NOT IN ('hpo_candidate','paper_trade_candidate',
                                        'infrastructure_failure')
           ORDER BY ranked.evaluated_at DESC, e.id DESC LIMIT ?""",
        (max(limit, CONTEXT_FAILURE_LIMIT),),
    )
    promising = [
        _compact_recent_item(database, row, evidence_limit=2)
        for row in recent if row["verdict"] in REVISION_VERDICTS
    ][:limit]
    diagnosed_failures = [
        _compact_recent_item(database, row, evidence_limit=2)
        for row in recent if row["verdict"] == "reject"
    ][:CONTEXT_FAILURE_LIMIT]
    stable_fingerprints = database.rows(
        """WITH latest AS (
               SELECT ev.*, ROW_NUMBER() OVER (
                   PARTITION BY ev.experiment_id ORDER BY ev.evaluated_at DESC, ev.id DESC
               ) AS rn
               FROM evaluations ev
           ), tested AS (
               SELECT json_extract(e.specification_json,'$.entry_rule.fingerprint') AS fingerprint,
                      COUNT(DISTINCT e.id) AS tested_count,
                      MAX(latest.evaluated_at) AS last_evaluated_at
               FROM experiments e JOIN latest ON latest.experiment_id=e.id AND latest.rn=1
               WHERE json_extract(e.specification_json,'$.entry_rule.fingerprint') IS NOT NULL
                 AND EXISTS (SELECT 1 FROM normalized_evidence n
                             WHERE n.experiment_id=e.id)
                 AND latest.verdict NOT IN ('hpo_candidate','paper_trade_candidate',
                                             'infrastructure_failure')
               GROUP BY fingerprint
           )
           SELECT fingerprint, tested_count, last_evaluated_at
           FROM tested ORDER BY tested_count DESC,last_evaluated_at DESC,fingerprint
           LIMIT ?""",
        (CONTEXT_FINGERPRINT_LIMIT,),
    )
    fingerprint_count = database.rows(
        """SELECT COUNT(DISTINCT json_extract(specification_json,'$.entry_rule.fingerprint')) AS count
           FROM experiments
           WHERE json_extract(specification_json,'$.entry_rule.fingerprint') IS NOT NULL"""
    )[0]["count"]
    themes = database.rows(
        """WITH ranked AS (
               SELECT ev.*, ROW_NUMBER() OVER (
                   PARTITION BY ev.experiment_id ORDER BY ev.evaluated_at DESC, ev.id DESC
               ) AS rn
               FROM evaluations ev
           )
           SELECT COALESCE(NULLIF(e.archetype,''),'unspecified') AS archetype,
                  COUNT(*) AS tested_count,
                  SUM(CASE WHEN ranked.verdict IN ('revise','inconclusive') THEN 1 ELSE 0 END)
                    AS promising_count,
                  SUM(CASE WHEN ranked.verdict='reject' THEN 1 ELSE 0 END)
                    AS failure_count
           FROM ranked JOIN experiments e ON e.id=ranked.experiment_id
           WHERE ranked.rn=1
             AND ranked.verdict NOT IN ('hpo_candidate','paper_trade_candidate',
                                        'infrastructure_failure')
           GROUP BY COALESCE(NULLIF(e.archetype,''),'unspecified')
           ORDER BY tested_count DESC, archetype LIMIT ?""",
        (CONTEXT_THEME_LIMIT,),
    )
    context = {
        "context_schema_version": 2,
        "inspect_limit": limit, "generate_limit": policy.synthesis_generate_limit,
        "evidence_rows_per_candidate": evidence_limit,
        "low_watermark": policy.synthesis_low_watermark,
        "active_ready_limit": policy.active_ready_limit,
        "max_revision_depth": policy.synthesis_max_revision_depth,
        "lane_policy": {
            "minimum_new_concepts": policy.synthesis_min_new_concepts,
            "maximum_improvements": policy.synthesis_max_improvements,
            "allocation": "eligible improvements first; reserve new-concept floor; backfill either lane",
        },
        "allocation": {
            "exact_total": policy.synthesis_generate_limit,
            "new_concepts_at_least": policy.synthesis_min_new_concepts,
            "controlled_improvements_at_most": policy.synthesis_max_improvements,
            "new_concept_slots_reserved": policy.synthesis_min_new_concepts,
        },
        "resource_policy": policy.to_dict(),
        "improvement_candidates": revisions, "scheduled_candidates": scheduled,
        "concept_learnings": learnings,
        "promising_inconclusive": promising,
        "diagnosed_failures": diagnosed_failures,
        "failure_diagnoses": diagnosed_failures,
        "stable_tested_entry_fingerprints": [dict(row) for row in stable_fingerprints],
        "archetype_theme_representation": [dict(row) for row in themes],
        "archetype_coverage": [dict(row) for row in themes],
        "known_entry_fingerprint_count": int(fingerprint_count),
        "promotion_locked_count": locked_count,
        "forbidden_states": [
            "hpo_candidate and paper_trade_candidate are promotion-locked",
            "revision depth at or above max_revision_depth is not eligible",
            "requirements_pending work is not runnable",
            "infrastructure failures are retries, never research revisions",
            "duplicate entry fingerprints and duplicate cohort requests are rejected",
            "agent proposals cannot mutate SQLite state, evidence, leases, or gates",
        ],
        "forbidden_actions": [
            "revise hpo_candidate or paper_trade_candidate",
            "invent evidence or bypass significance gates",
            "repeat a tested entry-rule fingerprint",
        ],
        "portfolio_layer": _portfolio_context(database, policy),
        "agent_authority": "propose_only; canonical SQLite validation and gates decide",
    }
    if memory_adapter is None:
        context.update({
            "advisory_memory": [], "memory_degraded": True,
            "advisory_memory_provider": "honcho_unavailable",
            "authority": "advisory_only", "state_authority": "canonical_sqlite",
        })
    else:
        context.update(compact_advisory_memory(
            memory_adapter, context, max_items=5, max_bytes=8000,
            max_text_chars=600, max_queries=3, stop_on_failure=True,
        ))
        context["advisory_memory_provider"] = "honcho_v3_adapter"
    return context


def _compact_recent_item(
    database: WorkflowDatabase, row: Mapping[str, Any], *, evidence_limit: int,
) -> dict[str, Any]:
    item = {
        key: row[key] for key in (
            "experiment_id", "strategy", "archetype", "target_regime",
            "failure_regime", "verdict", "evaluated_at",
        ) if key in row
    }
    item["finding"] = str(row["summary"] or "")[:500]
    item["next_action"] = str(row["next_step"] or "")[:500]
    item["evidence"] = [
        evidence.to_compact_dict()
        for evidence in database.query_normalized_evidence(
            {"experiment_id": row["experiment_id"]}, limit=evidence_limit,
        )
    ]
    return item


def _portfolio_context(
    database: WorkflowDatabase, policy: ResourcePolicy,
) -> dict[str, Any]:
    """Expose paper candidates for a future portfolio preflight, read-only."""
    rows = database.rows(
        """WITH ranked AS (
               SELECT ev.*, ROW_NUMBER() OVER (
                   PARTITION BY ev.experiment_id ORDER BY ev.evaluated_at DESC, ev.id DESC
               ) AS rn
               FROM evaluations ev
           )
           SELECT e.id AS experiment_id, s.name AS strategy, ranked.evaluated_at
           FROM ranked JOIN experiments e ON e.id=ranked.experiment_id
           LEFT JOIN strategies s ON s.id=e.strategy_id
           WHERE ranked.rn=1 AND ranked.verdict='paper_trade_candidate'
           ORDER BY ranked.evaluated_at DESC, e.id LIMIT 25"""
    )
    return {
        "status": "advisory_preflight_only",
        "candidate_count": len(rows),
        "candidates": [
            {
                "experiment_id": str(row["experiment_id"]),
                "strategy": str(row["strategy"] or ""),
                "evaluated_at": str(row["evaluated_at"] or "")[:60],
                "correlation_status": "requires_aligned_return_series",
                "capacity_status": "requires_operator_capacity_input",
            }
            for row in rows
        ],
        "policy": {
            "correlation_threshold": policy.portfolio_correlation_threshold,
            "capacity_utilization_limit": policy.portfolio_capacity_utilization_limit,
        },
        "authority": "advisory_only; does not promote or schedule work",
    }


def apply_batch(
    database: WorkflowDatabase, payloads: list[dict[str, Any]], *, source_path: str = "agent-batch-synthesis",
    policy: ResourcePolicy | None = None, cohort_id: str | None = None,
) -> dict[str, Any]:
    """Validate and persist one planning batch with promotion locks and capacity control."""
    policy = policy or ResourcePolicy()
    payloads = [_normalize_typed_proposal(raw) for raw in payloads]
    payloads = _canonicalize_non_entry_rules(database, payloads)
    generate_limit = policy.synthesis_generate_limit
    generated: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    if cohort_id is not None and len(payloads) != generate_limit:
        raise ValueError(f"synthesis cohort requires exactly {generate_limit} requests")
    parsed: list[SynthesisRequest] = []
    if cohort_id is not None:
        new_fingerprints: set[str] = set()
        for index, raw_payload in enumerate(payloads):
            payload = dict(raw_payload)
            _validate_typed_proposal_payload(payload)
            payload.setdefault("n_simulations", policy.significance_simulations)
            payload["cohort_id"] = cohort_id
            payload["cohort_slot"] = index
            request = synthesis_request_from_payload(payload)
            _validate_cohort_request(database, request, cohort_id, index)
            key = (request.strategy_name.casefold(), request.job_fingerprint, request.change_scope)
            if key in seen:
                raise ValueError("duplicate request in synthesis batch")
            if request.action == "new" and request.entry_fingerprint in new_fingerprints:
                raise ValueError("duplicate new entry fingerprint in synthesis batch")
            if request.action == "new":
                new_fingerprints.add(request.entry_fingerprint)
            seen.add(key)
            _validate_source(
                database, request,
                max_revision_depth=policy.synthesis_max_revision_depth,
            )
            parsed.append(request)
        new_count = sum(request.lane == "new_concept" for request in parsed)
        improvement_count = sum(request.lane == "improvement" for request in parsed)
        if new_count < policy.synthesis_min_new_concepts:
            raise ValueError(
                f"synthesis cohort requires at least {policy.synthesis_min_new_concepts} new concepts"
            )
        if improvement_count > policy.synthesis_max_improvements:
            raise ValueError(
                f"synthesis cohort allows at most {policy.synthesis_max_improvements} improvements"
            )
        seen.clear()
    for index, raw_payload in enumerate(payloads[:generate_limit]):
        try:
            if parsed:
                request = parsed[index]
            else:
                payload = dict(raw_payload)
                payload.setdefault("n_simulations", policy.significance_simulations)
                request = synthesis_request_from_payload(payload)
            key = (request.strategy_name.casefold(), request.job_fingerprint, request.change_scope)
            if key in seen:
                raise ValueError("duplicate request in synthesis batch")
            seen.add(key)
            _validate_source(
                database, request,
                max_revision_depth=policy.synthesis_max_revision_depth,
            )
            active = database.rows(
                "SELECT COUNT(*) AS count FROM work_items WHERE state IN ('ready','running')"
            )[0]["count"]
            result = synthesize(
                database, request, source_path=source_path,
                release_ready=int(active) < policy.active_ready_limit,
            )
            result["action"] = request.action
            result["source_experiment_id"] = request.source_experiment_id
            result["lane"] = request.lane
            result["cohort_slot"] = request.cohort_slot
            generated.append(result)
        except (KeyError, TypeError, ValueError) as error:
            rejected.append({"index": index, "reason": str(error)})
    if len(payloads) > generate_limit:
        rejected.append({"index": generate_limit, "reason": f"batch exceeds generation limit {generate_limit}"})
    return {"generated": generated, "rejected": rejected, "submitted": len(payloads)}


def _normalize_typed_proposal(payload: dict[str, Any]) -> dict[str, Any]:
    """Map richer agent proposal names onto existing synthesis request fields."""
    result = dict(payload)
    proposal_type = result.get("type")
    if proposal_type == "new_concept":
        result.setdefault("action", "new")
        result.setdefault("lane", "new_concept")
    elif proposal_type == "controlled_improvement":
        result.setdefault("action", "revise")
        result.setdefault("lane", "improvement")
    if result.get("thesis"):
        result.setdefault("hypothesis", result["thesis"])
        result.setdefault("edge_thesis", result["thesis"])
    if result.get("entry_rule_summary"):
        result.setdefault("entry_rule", result["entry_rule_summary"])
    if proposal_type == "new_concept":
        result.setdefault("change_scope", "new_entry")
    elif proposal_type == "controlled_improvement":
        result.setdefault("change_scope", "entry_changed")
    return result


def _validate_typed_proposal_payload(payload: dict[str, Any]) -> None:
    # Older direct Worker callers use the pre-typed request contract. Preserve
    # that compatibility; any proposal using the richer contract is strict.
    if not TYPED_CONTRACT_MARKERS.intersection(payload):
        return
    missing = [
        field for field in TYPED_PROPOSAL_FIELDS
        if field not in payload
    ]
    if missing:
        raise ValueError("typed proposal missing fields: " + ", ".join(missing))
    proposal_type = payload["type"]
    if proposal_type not in {"new_concept", "controlled_improvement"}:
        raise ValueError("typed proposal type must be new_concept or controlled_improvement")
    if proposal_type == "new_concept":
        if payload["source_experiment_id"] is not None:
            raise ValueError("new concept source_experiment_id must be null")
    else:
        if not payload["source_experiment_id"]:
            raise ValueError("controlled improvement requires source_experiment_id")
        if not str(payload["controlled_change"] or "").strip():
            raise ValueError("controlled improvement requires controlled_change")
    for field in (
        "thesis", "archetype", "target_regime", "failure_regime",
        "falsifiability_criteria", "entry_rule_summary", "why_this_now",
        "expected_edge_type",
    ):
        if not str(payload[field] or "").strip():
            raise ValueError(f"typed proposal requires {field}")


def _canonicalize_non_entry_rules(
    database: WorkflowDatabase,
    payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Force non-entry revisions to retain source's canonical entry rule."""
    result = []
    for raw in payloads:
        payload = dict(raw)
        if (
            payload.get("action") == "revise"
            and payload.get("change_scope")
            in {"exit_only", "sizing_only", "risk_only", "refactor"}
            and payload.get("source_experiment_id")
        ):
            rows = database.rows(
                """SELECT
                     json_extract(
                       specification_json,'$.entry_rule.description'
                     ) AS description,
                     json_extract(
                       specification_json,'$.entry_rule.fingerprint'
                     ) AS fingerprint
                   FROM experiments WHERE id=?""",
                (payload["source_experiment_id"],),
            )
            if rows and rows[0]["description"]:
                payload["entry_rule"] = rows[0]["description"]
                payload["parent_entry_fingerprint"] = rows[0]["fingerprint"]
        result.append(payload)
    return result


def _validate_cohort_request(
    database: WorkflowDatabase, request: SynthesisRequest, cohort_id: str, index: int,
) -> None:
    if request.cohort_id != cohort_id or request.cohort_slot != index:
        raise ValueError("cohort identity or slot mismatch")
    required = {
        "archetype": request.archetype,
        "target_regime": request.target_regime,
        "failure_regime": request.failure_regime,
        "edge_thesis": request.edge_thesis,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise ValueError("cohort request missing fields: " + ", ".join(missing))
    expected_lane = "improvement" if request.action == "revise" else "new_concept"
    if request.lane != expected_lane:
        raise ValueError(f"{request.action} action requires {expected_lane} lane")
    if request.action == "new":
        rows = database.rows(
            """SELECT 1 FROM experiments
               WHERE json_extract(specification_json,'$.entry_rule.fingerprint')=? LIMIT 1""",
            (request.entry_fingerprint,),
        )
        if rows:
            raise ValueError("new concept duplicates existing entry fingerprint")
    elif request.change_scope in {"exit_only", "sizing_only", "risk_only", "refactor"}:
        rows = database.rows(
            """SELECT json_extract(specification_json,'$.entry_rule.fingerprint') AS fingerprint
               FROM experiments WHERE id=?""",
            (request.source_experiment_id,),
        )
        source_fingerprint = rows[0]["fingerprint"] if rows else None
        if source_fingerprint and source_fingerprint != request.entry_fingerprint:
            raise ValueError("non-entry improvement must retain source entry rule")


def _validate_source(
    database: WorkflowDatabase,
    request: SynthesisRequest,
    *,
    max_revision_depth: int = MAX_REVISION_DEPTH,
) -> None:
    if request.action == "new":
        if request.source_experiment_id:
            raise ValueError("new action must not set source_experiment_id")
        return
    source = request.source_experiment_id or ""
    if is_promotion_locked(database, source):
        raise ValueError(f"promotion-locked experiment cannot be revised: {source}")
    rows = database.rows(
        """SELECT verdict FROM evaluations WHERE experiment_id=?
           ORDER BY evaluated_at DESC,id DESC LIMIT 1""",
        (source,),
    )
    if not rows or rows[0]["verdict"] not in REVISION_VERDICTS:
        raise ValueError(f"revision source must have latest revise/inconclusive verdict: {source}")
    depth = revision_depth(database, source)
    if depth >= max_revision_depth:
        raise ValueError(f"revision depth limit reached for {source}: {depth}")
