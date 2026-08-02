"""Safe advisory research memory derived only from canonical ATS evidence."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol, TYPE_CHECKING

from .models import Evaluation, utc_now

if TYPE_CHECKING:
    from .database import WorkflowDatabase


SCHEMA_VERSION = 1
MAX_PAYLOAD_BYTES = 8192
MAX_REASON_CODES = 12
_TEXT_LIMITS = {
    "strategy": 120,
    "archetype": 120,
    "hypothesis": 300,
    "target_regime": 240,
    "failure_regime": 240,
    "lesson": 700,
    "next_refinement_constraint": 500,
}
_FORBIDDEN_TEXT = re.compile(
    r"https?://|\b(?:api[_ -]?key|password|credential|auth[_ -]?token|"
    r"session[_ -]?id|strategy[_ -]?source|traceback)\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|"
    r"\b(?:def|class)\s+[A-Za-z_]\w*\s*[(:]",
    re.IGNORECASE,
)
_CHANGE_SCOPE = {
    "new_entry": "new_entry", "entry": "entry", "entry_changed": "entry",
    "exit": "exit", "exit_only": "exit", "risk": "risk",
    "risk_only": "risk", "sizing": "sizing", "sizing_only": "sizing",
    "refactor": "refactor",
}
_LIFECYCLE = {
    "significance": "significance", "baseline": "baseline",
    "out_of_sample": "oos", "cost_sensitivity": "cost", "hpo": "hpo",
    "monte_carlo": "monte_carlo", "multi_window": "baseline",
    "harness_check": "baseline", "paper_trade": "paper_trade",
}
_METRICS = (
    "trade_count", "net_profit_percentage", "max_drawdown_percentage",
    "sharpe_ratio", "profit_factor", "significance_p_value",
)
_RECALL_REQUIRED = {
    "schema_version", "learning_id", "experiment_id", "strategy", "archetype",
    "change_scope", "target_regime", "failure_regime", "lifecycle_stage",
    "verdict", "reason_codes", "normalized_metrics", "lesson",
    "next_refinement_constraint", "evaluated_at",
}


class ResearchMemoryAdapter(Protocol):
    def deliver(self, payload: dict[str, Any]) -> None: ...
    def recall(self, query: str, *, limit: int) -> list[dict[str, Any]]: ...


class MemoryProviderError(RuntimeError):
    pass


def _text(field: str, value: object) -> str:
    text = " ".join(str(value or "").split())
    if _FORBIDDEN_TEXT.search(text):
        raise ValueError(f"unsafe {field} text")
    return text[:_TEXT_LIMITS[field]]


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _reason_codes(verdict: str, evidence: list[dict[str, Any]]) -> list[str]:
    codes: list[str] = []
    if verdict == "reject":
        codes.append("deterministic_gate_failed")
    elif verdict == "revise":
        codes.append("revision_required")
    elif verdict in {"hpo_candidate", "paper_trade_candidate", "pass"}:
        codes.append("deterministic_gate_passed")
    else:
        codes.append("evidence_inconclusive")
    findings = " ".join(str(row.get("finding") or "") for row in evidence)
    for name in (
        "route_completion", "minimum_trades", "net_profit", "max_drawdown",
        "sharpe", "profit_factor", "fees_cost_sensitivity",
        "train_holdout_degradation",
    ):
        if name in findings:
            codes.append(f"gate_{name}")
    return list(dict.fromkeys(codes))[:MAX_REASON_CODES]


def _normalized_metrics(evidence: list[dict[str, Any]]) -> dict[str, int | float]:
    result: dict[str, int | float] = {}
    for field in _METRICS:
        values = [row[field] for row in evidence if row.get(field) is not None]
        for value in values:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"non-finite or invalid normalized metric: {field}")
        unique = list(dict.fromkeys(values))
        if len(unique) == 1:
            result[field] = unique[0]
    return result


def _reject_nonfinite_numbers(value: object) -> None:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            raise ValueError("non-finite canonical run metric")
        return
    if isinstance(value, list):
        for item in value:
            _reject_nonfinite_numbers(item)
        return
    if isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite_numbers(item)


def build_learning_record(
    connection: sqlite3.Connection, evaluation: Evaluation,
) -> dict[str, Any]:
    experiment = connection.execute(
        """SELECT e.id,s.name AS strategy,e.experiment_type,e.hypothesis,
                  e.archetype,e.target_regime,e.failure_regime,
                  e.specification_json
           FROM experiments e LEFT JOIN strategies s ON s.id=e.strategy_id
           WHERE e.id=?""",
        (evaluation.experiment_id,),
    ).fetchone()
    if experiment is None:
        raise ValueError("learning requires canonical experiment")
    evidence = [dict(row) for row in connection.execute(
        """SELECT * FROM normalized_evidence WHERE experiment_id=?
           ORDER BY COALESCE(completed_at,''),evidence_key""",
        (evaluation.experiment_id,),
    ).fetchall()]
    if not evidence:
        raise ValueError("learning requires canonical normalized evidence")
    for row in connection.execute(
        "SELECT metrics_json FROM runs WHERE experiment_id=? AND metrics_json IS NOT NULL",
        (evaluation.experiment_id,),
    ).fetchall():
        _reject_nonfinite_numbers(json.loads(row["metrics_json"]))
    specification = json.loads(experiment["specification_json"] or "{}")
    entry_rule = specification.get("entry_rule")
    fingerprint = entry_rule.get("fingerprint") if isinstance(entry_rule, dict) else None
    if not fingerprint:
        description = (
            entry_rule.get("description") if isinstance(entry_rule, dict)
            else specification.get("entry_rule")
        )
        if description:
            fingerprint = hashlib.sha256(str(description).encode()).hexdigest()
    stage_raw = next(
        (row["lifecycle_stage"] for row in evidence if row.get("lifecycle_stage")),
        experiment["experiment_type"],
    )
    lifecycle = _LIFECYCLE.get(str(stage_raw), str(stage_raw))
    scope_raw = specification.get("change_scope") or specification.get("controlled_change")
    scope = _CHANGE_SCOPE.get(str(scope_raw or ""), "new_entry")
    lesson = _text("lesson", evaluation.summary)
    if lifecycle == "significance" and evaluation.verdict.value in {
        "pass", "hpo_candidate", "paper_trade_candidate",
    }:
        constraint = "Significance pass does not prove profitability."
        lesson = (lesson + " " + constraint).strip()[:_TEXT_LIMITS["lesson"]]
    material: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": evaluation.experiment_id[:160],
        "strategy": _text("strategy", experiment["strategy"]),
        "archetype": _text("archetype", experiment["archetype"]),
        "change_scope": scope,
        "hypothesis": _text("hypothesis", experiment["hypothesis"]),
        "target_regime": _text("target_regime", experiment["target_regime"]),
        "failure_regime": _text("failure_regime", experiment["failure_regime"]),
        "lifecycle_stage": lifecycle,
        "verdict": evaluation.verdict.value,
        "reason_codes": _reason_codes(evaluation.verdict.value, evidence),
        "normalized_metrics": _normalized_metrics(evidence),
        "lesson": lesson,
        "next_refinement_constraint": _text(
            "next_refinement_constraint", evaluation.next_step,
        ),
        "evaluated_at": evaluation.evaluated_at,
        "evidence_strength": "canonical_evidence_with_deterministic_gates",
        "claim_types": [
            "observed_fact", "deterministic_gate_result",
            "analyst_interpretation", "proposed_refinement",
        ],
        "evidence_routes": [
            {
                key: row[key] for key in (
                    "symbol", "timeframe", "evidence_split",
                    "start_date", "finish_date",
                ) if row.get(key) is not None
            }
            for row in evidence[:4]
        ],
    }
    if fingerprint:
        material["entry_rule_fingerprint"] = str(fingerprint)[:128]
    learning_id = hashlib.sha256(_stable_json(material).encode()).hexdigest()
    payload = {**material, "learning_id": learning_id}
    if len(_stable_json(payload).encode()) > MAX_PAYLOAD_BYTES:
        raise ValueError("learning payload exceeds byte limit")
    return payload


def enqueue_learning(
    connection: sqlite3.Connection, evaluation: Evaluation,
) -> dict[str, Any]:
    payload = build_learning_record(connection, evaluation)
    fingerprint = payload["learning_id"]
    connection.execute(
        """INSERT OR IGNORE INTO research_memory_outbox(
               learning_fingerprint,payload_json,state,created_at
           ) VALUES (?,?,'pending',?)""",
        (fingerprint, _stable_json(payload), utc_now()),
    )
    return payload


def memory_status(database: WorkflowDatabase) -> dict[str, int]:
    rows = database.rows(
        "SELECT state,COUNT(*) AS count FROM research_memory_outbox GROUP BY state"
    )
    counts = {row["state"]: int(row["count"]) for row in rows}
    return {state: counts.get(state, 0) for state in ("pending", "retry", "delivered")}


def _error_code(error: Exception) -> str:
    if isinstance(error, MemoryProviderError):
        value = str(error)
        return value if re.fullmatch(r"[a-z0-9_]{1,80}", value) else "memory_error"
    if isinstance(error, (OSError, TimeoutError, urllib.error.URLError)):
        return "memory_transport_error"
    return "memory_delivery_error"


def sync_memory_outbox(
    database: WorkflowDatabase,
    adapter: ResearchMemoryAdapter,
    *,
    apply: bool = False,
    limit: int = 25,
) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    now = utc_now()
    rows = database.rows(
        """SELECT id,learning_fingerprint,payload_json,attempts,state
           FROM research_memory_outbox
           WHERE state='pending' OR (
             state='retry' AND (retry_after IS NULL OR retry_after<=?)
           ) ORDER BY id LIMIT ?""",
        (now, limit),
    )
    result: dict[str, Any] = {
        "apply": apply, "eligible": len(rows), "delivered": 0, "retry": 0,
        "learning_fingerprints": [row["learning_fingerprint"] for row in rows],
    }
    if not apply:
        return result
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            adapter.deliver(payload)
        except Exception as error:  # adapter boundary; persisted detail stays sanitized
            attempts = int(row["attempts"] or 0) + 1
            retry_after = (
                datetime.now(timezone.utc)
                + timedelta(seconds=min(3600, 30 * (2 ** min(attempts - 1, 7))))
            ).isoformat().replace("+00:00", "Z")
            with database.connect() as connection:
                connection.execute(
                    """UPDATE research_memory_outbox SET state='retry',attempts=?,
                              retry_after=?,last_error_code=? WHERE id=?""",
                    (attempts, retry_after, _error_code(error), row["id"]),
                )
            result["retry"] += 1
            continue
        with database.connect() as connection:
            connection.execute(
                """UPDATE research_memory_outbox SET state='delivered',
                          attempts=attempts+1,retry_after=NULL,last_error_code=NULL,
                          delivered_at=? WHERE id=?""",
                (utc_now(), row["id"]),
            )
        result["delivered"] += 1
    return result


def _recall_queries(context: Mapping[str, Any]) -> list[str]:
    strategy_names: list[str] = []
    archetype_terms: list[str] = []
    regime_terms: list[str] = []
    refinement_terms: list[str] = []
    for collection in (
        "improvement_candidates", "scheduled_candidates", "concept_learnings",
    ):
        for item in context.get(collection, []) if isinstance(context.get(collection), list) else []:
            if not isinstance(item, dict):
                continue
            specification = item.get("specification_json")
            try:
                specification = json.loads(specification) if isinstance(specification, str) else specification
            except json.JSONDecodeError:
                specification = {}
            groups = (
                (strategy_names, (item.get("strategy"),)),
                (archetype_terms, (item.get("archetype"),)),
                (regime_terms, (item.get("target_regime"), item.get("failure_regime"))),
                (refinement_terms, (
                    item.get("source_experiment_id"),
                    specification.get("change_scope") if isinstance(specification, dict) else None,
                    item.get("verdict"),
                )),
            )
            for target, values in groups:
                for value in values:
                    if value:
                        target.append(" ".join(str(value).split())[:120])
            for evidence in item.get("evidence", []) if isinstance(item.get("evidence"), list) else []:
                if isinstance(evidence, dict) and evidence.get("finding"):
                    refinement_terms.append(
                        " ".join(str(evidence["finding"]).split())[:160]
                    )
    queries = [
        f"ATS strategy learnings {strategy}"
        for strategy in dict.fromkeys(strategy_names)
    ][:3]
    for prefix, terms in (
        ("ATS strategy archetype evidence", archetype_terms),
        ("ATS regime evidence", regime_terms),
        ("ATS refinement evidence", refinement_terms),
    ):
        unique = " ".join(dict.fromkeys(terms))[:600]
        if unique:
            queries.append(f"{prefix} {unique}")
    return queries or ["ATS evidence-derived strategy learning"]


def compact_advisory_memory(
    adapter: ResearchMemoryAdapter,
    context: Mapping[str, Any],
    *,
    max_items: int = 5,
    max_bytes: int = 8000,
    max_text_chars: int = 600,
) -> dict[str, Any]:
    existing = {
        str(item.get(key))
        for collection, key in (
            ("improvement_candidates", "source_experiment_id"),
            ("scheduled_candidates", "source_experiment_id"),
            ("concept_learnings", "experiment_id"),
        )
        for item in (context.get(collection) or [])
        if isinstance(item, dict) and item.get(key)
    }
    recalled: list[dict[str, Any]] = []
    recall_failed = False
    for query in _recall_queries(context):
        try:
            batch = adapter.recall(query, limit=max_items * 2)
        except Exception:
            recall_failed = True
            continue
        if not isinstance(batch, list):
            recall_failed = True
            continue
        recalled.extend(item for item in batch if isinstance(item, dict))
    if not recalled and recall_failed:
        return {"advisory_memory": [], "memory_degraded": True}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    degraded = recall_failed
    for raw in recalled:
        if not isinstance(raw, dict) or not _RECALL_REQUIRED <= set(raw):
            degraded = True
            continue
        if not isinstance(raw.get("lesson"), str) or not isinstance(raw.get("reason_codes"), list):
            degraded = True
            continue
        learning_id = str(raw["learning_id"])
        experiment_id = str(raw["experiment_id"])
        if learning_id in seen or experiment_id in existing:
            continue
        item = {
            "trust": "untrusted_advisory_data",
            "learning_id": learning_id[:128],
            "experiment_id": experiment_id[:160],
            "strategy": str(raw["strategy"])[:120],
            "archetype": str(raw["archetype"])[:120],
            "target_regime": str(raw["target_regime"])[:max_text_chars],
            "failure_regime": str(raw["failure_regime"])[:max_text_chars],
            "change_scope": str(raw["change_scope"])[:40],
            "lifecycle_stage": str(raw["lifecycle_stage"])[:40],
            "historical_verdict": str(raw["verdict"])[:40],
            "reason_codes": [str(code)[:80] for code in raw["reason_codes"][:MAX_REASON_CODES]],
            "normalized_metrics": raw["normalized_metrics"] if isinstance(raw["normalized_metrics"], dict) else {},
            "lesson": raw["lesson"][:max_text_chars],
            "next_refinement_constraint": str(raw["next_refinement_constraint"])[:max_text_chars],
            "evaluated_at": str(raw["evaluated_at"])[:60],
            "evidence_strength": str(raw.get("evidence_strength") or "canonical_evidence")[:80],
            "claim_types": [
                str(value)[:80] for value in (raw.get("claim_types") or [])[:4]
            ] if isinstance(raw.get("claim_types"), list) else [],
            "evidence_routes": [
                {
                    key: str(route[key])[:80]
                    for key in (
                        "symbol", "timeframe", "evidence_split",
                        "start_date", "finish_date",
                    ) if route.get(key) is not None
                }
                for route in (raw.get("evidence_routes") or [])[:4]
                if isinstance(route, dict)
            ],
        }
        candidate = [*items, item]
        if len(json.dumps(candidate, sort_keys=True).encode()) > max_bytes:
            break
        items.append(item)
        seen.add(learning_id)
        if len(items) >= max_items:
            break
    return {"advisory_memory": items, "memory_degraded": degraded}


@dataclass(frozen=True)
class MemoryProviderConfig:
    base_url: str = "http://127.0.0.1:18000"
    workspace_id: str = "ats-lab-memory"
    peer_id: str = "ats-lab-memory-peer"
    session_id: str = "strategy-learnings-v1"
    timeout_seconds: float = 30


class MemoryResearchAdapter:
    """Supported Memory v3 workspace/session message and hybrid-search adapter."""

    def __init__(
        self, config: MemoryProviderConfig | None = None, *, api_key: str | None = None,
    ) -> None:
        self.config = config or MemoryProviderConfig(
            base_url=os.environ.get("ATS_LAB_MEMORY_URL", "http://127.0.0.1:18000")
        )
        self.api_key = api_key if api_key is not None else os.environ.get("ATS_LAB_MEMORY_API_KEY")
        self._ensured = False

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        data = _stable_json(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            self.config.base_url.rstrip("/") + path,
            data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.config.timeout_seconds,
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise MemoryProviderError(f"memory_http_{error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise MemoryProviderError("memory_transport_error") from error
        try:
            return json.loads(body) if body else None
        except json.JSONDecodeError as error:
            raise MemoryProviderError("memory_malformed_response") from error

    @staticmethod
    def _quoted(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    def health(self) -> bool:
        response = self._request("GET", "/health")
        return isinstance(response, dict)

    def _ensure_namespace(self) -> None:
        if self._ensured:
            return
        workspace = self._quoted(self.config.workspace_id)
        self._request("POST", "/v3/workspaces", {"id": self.config.workspace_id})
        self._request("POST", f"/v3/workspaces/{workspace}/peers", {
            "id": self.config.peer_id,
            "metadata": {"namespace": "ats_research", "kind": "strategy_learning"},
        })
        self._request("POST", f"/v3/workspaces/{workspace}/sessions", {
            "id": self.config.session_id,
            "metadata": {"namespace": "ats_research", "kind": "strategy_learnings"},
        })
        self._ensured = True

    def _search(self, query: str, limit: int) -> list[dict[str, Any]]:
        workspace = self._quoted(self.config.workspace_id)
        session = self._quoted(self.config.session_id)
        response = self._request(
            "POST", f"/v3/workspaces/{workspace}/sessions/{session}/search",
            {"query": query, "limit": max(1, min(limit, 100))},
        )
        return response if isinstance(response, list) else []

    def _messages_by_fingerprint(self, learning_id: str) -> list[dict[str, Any]]:
        workspace = self._quoted(self.config.workspace_id)
        session = self._quoted(self.config.session_id)
        response = self._request(
            "POST",
            f"/v3/workspaces/{workspace}/sessions/{session}/messages/list?page=1&size=5",
            {"filters": {"metadata": {"learning_fingerprint": learning_id}}},
        )
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            return []
        return [item for item in response["items"] if isinstance(item, dict)]

    @staticmethod
    def _payload_from_message(message: Mapping[str, Any]) -> dict[str, Any] | None:
        content = message.get("content")
        if not isinstance(content, str):
            return None
        marker = "Evidence-derived ATS research learning\n"
        if not content.startswith(marker):
            return None
        try:
            payload = json.loads(content[len(marker):])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def deliver(self, payload: dict[str, Any]) -> None:
        self._ensure_namespace()
        learning_id = str(payload["learning_id"])
        for message in self._messages_by_fingerprint(learning_id):
            existing = self._payload_from_message(message)
            if existing and existing.get("learning_id") == learning_id:
                return
        workspace = self._quoted(self.config.workspace_id)
        session = self._quoted(self.config.session_id)
        content = "Evidence-derived ATS research learning\n" + _stable_json(payload)
        self._request(
            "POST", f"/v3/workspaces/{workspace}/sessions/{session}/messages",
            {"messages": [{
                "content": content, "peer_id": self.config.peer_id,
                "metadata": {
                    "kind": "strategy_learning", "schema_version": SCHEMA_VERSION,
                    "learning_fingerprint": learning_id,
                },
            }]},
        )

    def recall(self, query: str, *, limit: int) -> list[dict[str, Any]]:
        self._ensure_namespace()
        result = []
        for message in self._search(query, limit):
            payload = self._payload_from_message(message)
            if payload is not None:
                result.append(payload)
        return result
