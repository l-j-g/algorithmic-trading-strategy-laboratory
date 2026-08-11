"""Typed, read-only backend snapshots for CLI and local web consumers."""
from __future__ import annotations

import json
import mimetypes
import sqlite3
from math import inf
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .dashboard import hpo_detail_snapshot, query_page
from .database import WorkflowDatabase
from .local_commands import LocalCommandError, LocalCommandRunner
from .loop_control import SupervisorLoopControl
from .status import operator_status


MAX_EVENT_LIMIT = 100


def _bounded_event_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit must be an integer")
    if limit < 1 or limit > MAX_EVENT_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_EVENT_LIMIT}")
    return limit


def _as_int(value: object) -> int:
    return int(value or 0)


@dataclass(frozen=True)
class HealthSnapshot:
    """Small workflow health contract; never contains raw evidence or payloads."""

    healthy: bool
    checked_at: str
    progress_state: str
    next_action: str
    unresolved_execution_claims: int
    invalid_retry_schedules: int
    read_only: bool = True

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SummarySnapshot:
    """Canonical SQLite workflow summary shared by terminal and web callers."""

    checked_at: str
    healthy: bool
    progress_state: str
    next_action: str
    work_states: Mapping[str, int]
    active: int
    blocked: int
    awaiting_batch_evaluation: int
    running_execution_claims: int
    unresolved_execution_claims: int
    latest_event: str | None
    synthesis: Mapping[str, object]
    hpo: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class EventSnapshot:
    """Public event metadata. Event payload JSON stays diagnostic/internal."""

    id: int
    aggregate_type: str
    aggregate_id: str
    event_type: str
    occurred_at: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ApiSnapshot:
    health: HealthSnapshot
    summary: SummarySnapshot
    events: tuple[EventSnapshot, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "health": self.health.to_dict(),
            "summary": self.summary.to_dict(),
            "events": [event.to_dict() for event in self.events],
        }


class ReadOnlyApi:
    """Read-only service facade over canonical ``WorkflowDatabase`` state.

    Constructor deliberately does not call ``initialize``. Callers must point
    this service at an already-initialized canonical SQLite database; snapshots
    perform SELECTs only through ``WorkflowDatabase.rows``.
    """

    def __init__(
        self, database: WorkflowDatabase, *, claim_timeout_seconds: int = 7200,
    ) -> None:
        self.database = database
        self.claim_timeout_seconds = claim_timeout_seconds

    def _status(self) -> dict[str, Any]:
        return operator_status(
            self.database, claim_timeout_seconds=self.claim_timeout_seconds,
        )

    @staticmethod
    def _health_from_status(status: Mapping[str, object]) -> HealthSnapshot:
        return HealthSnapshot(
            healthy=bool(status["healthy"]),
            checked_at=str(status["checked_at"]),
            progress_state=str(status["progress_state"]),
            next_action=str(status["next_action"]),
            unresolved_execution_claims=_as_int(
                status["unresolved_execution_claims"]
            ),
            invalid_retry_schedules=_as_int(status["invalid_retry_schedules"]),
        )

    @staticmethod
    def _summary_from_status(status: Mapping[str, Any]) -> SummarySnapshot:
        work_states = {
            str(key): _as_int(value)
            for key, value in dict(status["work_states"]).items()
        }
        return SummarySnapshot(
            checked_at=str(status["checked_at"]),
            healthy=bool(status["healthy"]),
            progress_state=str(status["progress_state"]),
            next_action=str(status["next_action"]),
            work_states=work_states,
            active=_as_int(status["active"]),
            blocked=_as_int(status["blocked"]),
            awaiting_batch_evaluation=_as_int(
                status["awaiting_batch_evaluation"]
            ),
            running_execution_claims=_as_int(status["running_execution_claims"]),
            unresolved_execution_claims=_as_int(
                status["unresolved_execution_claims"]
            ),
            latest_event=(
                str(status["latest_event"])
                if status["latest_event"] is not None else None
            ),
            synthesis=dict(status["synthesis"]),
            hpo=dict(status["hpo"]),
        )

    def health_snapshot(self) -> HealthSnapshot:
        return self._health_from_status(self._status())

    def summary_snapshot(self) -> SummarySnapshot:
        return self._summary_from_status(self._status())

    def event_snapshots(self, limit: int = 20) -> tuple[EventSnapshot, ...]:
        limit = _bounded_event_limit(limit)
        rows = self.database.rows(
            """SELECT id,aggregate_type,aggregate_id,event_type,occurred_at
               FROM events ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        return tuple(
            EventSnapshot(
                id=int(row["id"]),
                aggregate_type=str(row["aggregate_type"]),
                aggregate_id=str(row["aggregate_id"]),
                event_type=str(row["event_type"]),
                occurred_at=str(row["occurred_at"]),
            )
            for row in rows
        )

    def page_snapshot(
        self, page: str, params: Mapping[str, str] | None = None,
    ) -> dict[str, object]:
        """Return an existing bounded dashboard projection as JSON data."""
        if page not in {"queue", "candidates", "runs"}:
            raise ValueError("unsupported page")
        rows, filters = query_page(self.database, page, dict(params or {}))
        return {"page": page, "filters": filters, "rows": rows}

    def hpo_snapshot(self, params: Mapping[str, str] | None = None) -> dict[str, object]:
        params = dict(params or {})
        filters = {
            key: params[key]
            for key in ("lifecycle_state", "strategy")
            if params.get(key)
        }
        query = getattr(self.database, "hpo_studies", None)
        rows = query(filters=filters, limit=500) if query is not None else []
        return {"page": "hpo", "filters": filters, "rows": rows}

    def hpo_detail(self, study_id: str) -> dict[str, object] | None:
        return hpo_detail_snapshot(self.database, study_id)

    def control_snapshot(self) -> dict[str, object]:
        """Return durable supervisor intent and last published runtime state."""
        return {
            "control": self.database.control_status(),
            "supervisor": self.database.supervisor_runtime_status(),
        }

    def attention_snapshot(self) -> dict[str, object]:
        status = self._status()
        items: list[dict[str, object]] = []
        unresolved = _as_int(status["unresolved_execution_claims"])
        if unresolved:
            items.append({
                "id": "execution-claims",
                "kind": "execution_claims",
                "severity": "critical",
                "title": f"{unresolved} unresolved execution claims",
                "detail": str(status["next_action"]),
                "next_action": "recover_or_inspect_running_claim",
                "resolution": "Inspect stale claims and durable run evidence before recovery.",
            })
        queue_rows, _ = query_page(self.database, "queue", {})
        for row in queue_rows:
            state = str(row.get("state") or "")
            if state not in {"blocked", "waiting_retry"}:
                continue
            items.append({
                "id": f"work-item:{row['id']}",
                "kind": "work_item",
                "work_item_id": row["id"],
                "severity": "critical" if state == "blocked" else "warning",
                "title": f"{state}: {row['id']}",
                "detail": row.get("blocker_detail") or row.get("blocker_code") or "Review queue item",
                "next_action": row.get("blocker_code") or "inspect_work_item",
                "resolution": "Open item detail for evidence and next-step resolution.",
            })
        return {"items": items[:100], "count": len(items)}

    def backtest_snapshot(self, params: Mapping[str, str] | None = None) -> dict[str, object]:
        params = dict(params or {})
        rows = self._enrich_evidence_rows(
            [item.to_dict() for item in self.database.query_normalized_evidence(limit=5000)]
        )
        query = params.get("q", "").strip()[:200].casefold()
        strategy = params.get("strategy", "").strip().casefold()
        exact_filters = {
            key: params.get(key, "").strip()
            for key in (
                "verdict", "symbol", "timeframe", "evidence_split",
                "lifecycle_stage", "test_type",
            )
            if params.get(key, "").strip()
        }
        try:
            minimum_trades = max(0, min(int(params.get("minimum_trades", "0")), 1_000_000))
        except ValueError:
            minimum_trades = 0
        searchable = (
            "strategy", "experiment_id", "run_id", "session_id", "finding", "next_action",
            "hypothesis", "test_type",
        )
        filtered = []
        for row in rows:
            if query and not any(query in str(row.get(key) or "").casefold() for key in searchable):
                continue
            if strategy and strategy not in str(row.get("strategy") or "").casefold():
                continue
            if any(str(row.get(key) or "") != value for key, value in exact_filters.items()):
                continue
            if minimum_trades and _as_int(row.get("trade_count")) < minimum_trades:
                continue
            filtered.append(row)
        sort = params.get("sort", "newest")
        if sort not in {"newest", "profit", "sharpe", "trades", "drawdown"}:
            sort = "newest"
        if sort == "profit":
            filtered.sort(key=lambda row: row.get("net_profit_percentage") if row.get("net_profit_percentage") is not None else -inf, reverse=True)
        elif sort == "sharpe":
            filtered.sort(key=lambda row: row.get("sharpe_ratio") if row.get("sharpe_ratio") is not None else -inf, reverse=True)
        elif sort == "trades":
            filtered.sort(key=lambda row: _as_int(row.get("trade_count")), reverse=True)
        elif sort == "drawdown":
            filtered.sort(key=lambda row: abs(float(row["max_drawdown_percentage"])) if row.get("max_drawdown_percentage") is not None else inf)
        else:
            filtered.sort(key=lambda row: row.get("completed_at") or "", reverse=True)
        try:
            limit = max(1, min(int(params.get("limit", "100")), 500))
        except ValueError:
            limit = 100
        metric_rows = [row for row in filtered if row.get("trade_count") is not None or row.get("net_profit_percentage") is not None]
        profits = [float(row["net_profit_percentage"]) for row in metric_rows if row.get("net_profit_percentage") is not None]
        trades = [int(row["trade_count"]) for row in metric_rows if row.get("trade_count") is not None]
        sharpes = [float(row["sharpe_ratio"]) for row in metric_rows if row.get("sharpe_ratio") is not None]
        drawdowns = [abs(float(row["max_drawdown_percentage"])) for row in metric_rows if row.get("max_drawdown_percentage") is not None]
        statistics = {
            "reported_runs": len(filtered),
            "metric_runs": len(metric_rows),
            "profitable_runs": sum(1 for value in profits if value > 0),
            "total_trades": sum(trades) if trades else 0,
            "total_profit_percentage": round(sum(profits), 4) if profits else None,
            "average_profit_percentage": round(sum(profits) / len(profits), 4) if profits else None,
            "best_profit_percentage": max(profits) if profits else None,
            "worst_drawdown_percentage": max(drawdowns) if drawdowns else None,
            "average_sharpe_ratio": round(sum(sharpes) / len(sharpes), 4) if sharpes else None,
        }
        return {
            "page": "backtests",
            "filters": {
                **exact_filters, "q": params.get("q", "").strip()[:200],
                "strategy": params.get("strategy", "").strip(),
                "minimum_trades": str(minimum_trades), "sort": sort,
            },
            "statistics": statistics,
            "options": self._backtest_options(rows),
            "test_type_summary": self._test_type_summary(filtered),
            "rows": filtered[:limit],
        }

    def _enrich_evidence_rows(self, rows: list[dict[str, object]]) -> list[dict[str, object]]:
        """Attach safe lineage metadata without exposing raw Jesse payloads."""
        if not rows:
            return []
        experiment_ids = sorted({str(row["experiment_id"]) for row in rows if row.get("experiment_id")})
        run_ids = sorted({str(row["run_id"]) for row in rows if row.get("run_id")})
        experiments: list[dict[str, object]] = []
        runs: list[dict[str, object]] = []
        for start in range(0, len(experiment_ids), 500):
            chunk = experiment_ids[start:start + 500]
            experiments.extend(self.database.rows(
                """SELECT e.id,e.experiment_type,e.hypothesis,e.archetype,
                          e.target_regime,e.failure_regime,e.specification_json,
                          s.name AS strategy
                   FROM experiments e LEFT JOIN strategies s ON s.id=e.strategy_id
                   WHERE e.id IN ({})""".format(",".join("?" for _ in chunk)),
                tuple(chunk),
            ))
        for start in range(0, len(run_ids), 500):
            chunk = run_ids[start:start + 500]
            runs.extend(self.database.rows(
                """SELECT id,status,dashboard_url,started_at,finished_at
                   FROM runs WHERE id IN ({})""".format(",".join("?" for _ in chunk)),
                tuple(chunk),
            ))
        experiment_by_id = {str(row["id"]): row for row in experiments}
        run_by_id = {str(row["id"]): row for row in runs}
        enriched = []
        for row in rows:
            result = dict(row)
            experiment = experiment_by_id.get(str(row.get("experiment_id")), {})
            run = run_by_id.get(str(row.get("run_id")), {})
            for key in ("hypothesis", "archetype", "target_regime", "failure_regime"):
                result[key] = experiment.get(key)
            result["experiment_type"] = experiment.get("experiment_type") or row.get("lifecycle_stage")
            try:
                specification = json.loads(experiment.get("specification_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                specification = {}
            result["test_type"] = self._test_type(result["experiment_type"], specification)
            result["dashboard_url"] = run.get("dashboard_url")
            result["run_status"] = run.get("status")
            result["run_started_at"] = run.get("started_at")
            result["run_finished_at"] = run.get("finished_at")
            enriched.append(result)
        return enriched

    @staticmethod
    def _backtest_options(rows: list[dict[str, object]]) -> dict[str, list[str]]:
        options: dict[str, set[str]] = {key: set() for key in ("strategies", "verdicts", "symbols", "timeframes", "splits", "test_types")}
        for row in rows:
            for key, field in (("strategies", "strategy"), ("verdicts", "verdict"), ("symbols", "symbol"), ("timeframes", "timeframe"), ("splits", "evidence_split"), ("test_types", "test_type")):
                if row.get(field):
                    options[key].add(str(row[field]))
        return {key: sorted(values) for key, values in options.items()}

    @staticmethod
    def _test_type(experiment_type: object, specification: Mapping[str, object]) -> str:
        operation = specification.get("operation")
        if operation:
            return str(operation)
        fallback = {
            "baseline": "backtest",
            "hpo": "hpo",
            "monte_carlo": "monte_carlo",
            "significance": "significance",
        }
        return fallback.get(str(experiment_type or ""), str(experiment_type or "unknown"))

    @staticmethod
    def _test_type_summary(rows: list[dict[str, object]]) -> dict[str, dict[str, int | str | None]]:
        summary: dict[str, dict[str, int | str | None]] = {}
        for row in rows:
            test_type = str(row.get("test_type") or "unknown")
            lane = summary.setdefault(test_type, {
                "reported": 0, "with_metrics": 0, "without_metrics": 0,
                "latest_completed_at": None,
            })
            lane["reported"] = int(lane["reported"]) + 1
            has_metrics = row.get("trade_count") is not None or row.get("net_profit_percentage") is not None
            lane["with_metrics" if has_metrics else "without_metrics"] = int(lane["with_metrics" if has_metrics else "without_metrics"]) + 1
            completed_at = row.get("completed_at")
            if completed_at and (lane["latest_completed_at"] is None or str(completed_at) > str(lane["latest_completed_at"])):
                lane["latest_completed_at"] = str(completed_at)
        return summary

    def work_item_detail(self, work_item_id: str) -> dict[str, object] | None:
        rows = self.database.rows(
            """SELECT w.*,e.experiment_type AS experiment_name,s.name AS strategy
               FROM work_items w JOIN experiments e ON e.id=w.experiment_id
               LEFT JOIN strategies s ON s.id=e.strategy_id WHERE w.id=?""",
            (work_item_id,),
        )
        if not rows:
            return None
        events = self.database.rows(
            """SELECT id,event_type,occurred_at FROM events
               WHERE aggregate_type='work_item' AND aggregate_id=?
               ORDER BY id DESC LIMIT 50""",
            (work_item_id,),
        )
        return {
            "work_item": rows[0],
            "stage_timings": self.database.work_item_stage_timings(work_item_id),
            "events": events,
            "evidence": [item.to_dict() for item in self.database.normalized_evidence_for_experiment(rows[0]["experiment_id"])],
        }

    def evidence_detail(self, run_id: str) -> dict[str, object] | None:
        evidence = [item.to_dict() for item in self.database.normalized_evidence_for_run(run_id)]
        rows = self.database.rows(
            """SELECT id,experiment_id,work_item_id,session_id,status,started_at,
                      finished_at,dashboard_url FROM runs WHERE id=?""",
            (run_id,),
        )
        if not rows and not evidence:
            return None
        evidence = self._enrich_evidence_rows(evidence)
        experiment_id = (rows[0]["experiment_id"] if rows else evidence[0].get("experiment_id"))
        experiment_payload = self.experiment_detail(
            str(experiment_id), include_evidence=False,
        ) if experiment_id else None
        experiment = (
            experiment_payload["experiment"]
            if experiment_payload is not None else None
        )
        return {"run": rows[0] if rows else None, "experiment": experiment, "evidence": evidence}

    def experiment_detail(
        self, experiment_id: str, *, include_evidence: bool = True,
    ) -> dict[str, object] | None:
        rows = self.database.rows(
            """SELECT e.id,e.experiment_type,e.hypothesis,e.archetype,
                      e.target_regime,e.failure_regime,e.parent_experiment_id,
                      e.specification_json,
                      s.name AS strategy
               FROM experiments e LEFT JOIN strategies s ON s.id=e.strategy_id
               WHERE e.id=?""",
            (experiment_id,),
        )
        if not rows:
            return None
        runs = self.database.rows(
            """SELECT id,session_id,status,dashboard_url,started_at,finished_at
               FROM runs WHERE experiment_id=? ORDER BY COALESCE(finished_at,started_at) DESC,id""",
            (experiment_id,),
        )
        evidence = (
            self._enrich_evidence_rows(
                [item.to_dict() for item in self.database.normalized_evidence_for_experiment(experiment_id)]
            )
            if include_evidence else []
        )
        experiment = dict(rows[0])
        try:
            specification = json.loads(experiment.pop("specification_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            specification = {}
        experiment["test_type"] = self._test_type(experiment.get("experiment_type"), specification)
        return {"experiment": experiment, "runs": runs, "evidence": evidence}

    def snapshot(self, event_limit: int = 20) -> ApiSnapshot:
        status = self._status()
        return ApiSnapshot(
            health=self._health_from_status(status),
            summary=self._summary_from_status(status),
            events=self.event_snapshots(event_limit),
        )


# Explicit alias keeps service naming discoverable for callers that prefer the
# longer form while retaining one implementation and one read-only boundary.
ReadOnlyApiService = ReadOnlyApi


class ControlService:
    """Loopback operator controls backed by the existing lifecycle boundary."""

    ACTIONS = frozenset(("start", "pause", "resume", "stop"))

    def __init__(
        self,
        database: WorkflowDatabase,
        repo: Path,
        *,
        lifecycle: SupervisorLoopControl | None = None,
    ) -> None:
        self.database = database
        self.lifecycle = lifecycle or SupervisorLoopControl(database, repo)

    def snapshot(self) -> dict[str, object]:
        return ReadOnlyApi(self.database).control_snapshot()

    def apply(self, action: str) -> dict[str, object]:
        if action not in self.ACTIONS:
            raise ValueError(f"unsupported control action: {action}")
        method = {
            "start": self.lifecycle.start,
            "pause": self.lifecycle.pause,
            "resume": self.lifecycle.start,
            "stop": self.lifecycle.stop,
        }[action]
        result = method().to_dict()
        return {
            "action": action,
            "result": result,
            **self.snapshot(),
        }

    def resolve_work_item(
        self,
        work_item_id: str,
        *,
        resolution_code: str,
        detail: str,
        evidence_ids: list[str] | None = None,
    ) -> dict[str, object]:
        result = self.database.resolve_blocked_work_item(
            work_item_id,
            resolution_code=resolution_code,
            detail=detail,
            evidence_ids=evidence_ids or [],
        )
        return {"action": "resolve", "work_item": result, **self.snapshot()}


def health_snapshot(
    database: WorkflowDatabase, *, claim_timeout_seconds: int = 7200,
) -> HealthSnapshot:
    return ReadOnlyApi(
        database, claim_timeout_seconds=claim_timeout_seconds,
    ).health_snapshot()


def summary_snapshot(
    database: WorkflowDatabase, *, claim_timeout_seconds: int = 7200,
) -> SummarySnapshot:
    return ReadOnlyApi(
        database, claim_timeout_seconds=claim_timeout_seconds,
    ).summary_snapshot()


def event_snapshots(
    database: WorkflowDatabase, limit: int = 20,
) -> tuple[EventSnapshot, ...]:
    return ReadOnlyApi(database).event_snapshots(limit)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _service(value: ReadOnlyApi | WorkflowDatabase) -> ReadOnlyApi:
    return value if isinstance(value, ReadOnlyApi) else ReadOnlyApi(value)


def make_handler(
    value: ReadOnlyApi | WorkflowDatabase,
    *,
    static_dir: Path | None = None,
    control_service: ControlService | None = None,
    command_runner: LocalCommandRunner | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Build GET-only API handler, optionally serving the Control Room shell."""

    api = _service(value)
    static_root = static_dir.resolve() if static_dir is not None else None

    class WebApiHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send_json(self, payload: object, status: int = 200) -> None:
            body = _json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _send_static(self, request_path: str) -> bool:
            if static_root is None:
                return False
            relative = unquote(request_path).lstrip("/") or "index.html"
            candidate = (static_root / relative).resolve()
            try:
                candidate.relative_to(static_root)
            except ValueError:
                self._error(404, "not_found", "static asset not found")
                return True
            if not candidate.is_file():
                self._error(404, "not_found", "static asset not found")
                return True
            body = candidate.read_bytes()
            content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
            return True

        def _error(self, status: int, code: str, detail: str) -> None:
            self._send_json({"error": {"code": code, "detail": detail}}, status)

        def do_GET(self) -> None:  # noqa: N802
            request = urlsplit(self.path)
            params = {
                key: values[-1]
                for key, values in parse_qs(
                    request.query, keep_blank_values=False,
                ).items()
            }
            try:
                if request.path in {"/health", "/api/health", "/api/v1/health"}:
                    self._send_json(api.health_snapshot().to_dict())
                elif request.path in {"/api/summary", "/api/v1/summary"}:
                    self._send_json(api.summary_snapshot().to_dict())
                elif request.path in {"/api/events", "/api/v1/events"}:
                    limit = int(params.get("limit", "20"))
                    self._send_json({
                        "events": [
                            event.to_dict()
                            for event in api.event_snapshots(limit)
                        ],
                    })
                elif request.path in {"/api/snapshot", "/api/v1/snapshot"}:
                    limit = int(params.get("limit", "20"))
                    self._send_json(api.snapshot(limit).to_dict())
                elif request.path in {
                    "/api/queue", "/api/candidates", "/api/runs",
                    "/api/v1/queue", "/api/v1/candidates", "/api/v1/runs",
                }:
                    page = request.path.rsplit("/", 1)[-1]
                    self._send_json(api.page_snapshot(page, params))
                elif request.path in {"/api/hpo-studies", "/api/v1/hpo/studies"}:
                    self._send_json(api.hpo_snapshot(params))
                elif request.path in {"/api/control", "/api/v1/control"}:
                    self._send_json(
                        control_service.snapshot()
                        if control_service is not None
                        else api.control_snapshot()
                    )
                elif request.path in {"/api/attention", "/api/v1/attention"}:
                    self._send_json(api.attention_snapshot())
                elif request.path in {"/api/backtests", "/api/v1/backtests"}:
                    self._send_json(api.backtest_snapshot(params))
                elif request.path.startswith("/api/v1/backtests/"):
                    run_id = unquote(request.path.removeprefix("/api/v1/backtests/"))
                    detail = api.evidence_detail(run_id)
                    if detail is None or "/" in run_id:
                        self._error(404, "not_found", "backtest not found")
                    else:
                        self._send_json(detail)
                elif request.path.startswith("/api/v1/experiments/"):
                    experiment_id = unquote(request.path.removeprefix("/api/v1/experiments/"))
                    detail = api.experiment_detail(experiment_id)
                    if detail is None or "/" in experiment_id:
                        self._error(404, "not_found", "experiment not found")
                    else:
                        self._send_json(detail)
                elif request.path.startswith("/api/v1/work-items/"):
                    work_item_id = unquote(request.path.removeprefix("/api/v1/work-items/"))
                    detail = api.work_item_detail(work_item_id)
                    if detail is None or "/" in work_item_id:
                        self._error(404, "not_found", "work item not found")
                    else:
                        self._send_json(detail)
                elif request.path.startswith("/api/v1/evidence/"):
                    run_id = unquote(request.path.removeprefix("/api/v1/evidence/"))
                    detail = api.evidence_detail(run_id)
                    if detail is None or "/" in run_id:
                        self._error(404, "not_found", "evidence not found")
                    else:
                        self._send_json(detail)
                elif request.path.startswith("/api/v1/hpo/studies/"):
                    study_id = request.path.removeprefix("/api/v1/hpo/studies/")
                    detail = api.hpo_detail(study_id)
                    if detail is None or "/" in study_id:
                        self._error(404, "not_found", "HPO study not found")
                    else:
                        self._send_json(detail)
                elif self._send_static(request.path):
                    return
                else:
                    self._error(404, "not_found", "endpoint not found")
            except ValueError as error:
                self._error(400, "invalid_request", str(error))
            except (OSError, sqlite3.Error):
                self._error(503, "backend_unavailable", "SQLite snapshot unavailable")

        def _reject_mutation(self) -> None:
            body = _json_bytes({
                "error": {
                    "code": "method_not_allowed",
                    "detail": "read-only API accepts GET only",
                },
            })
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _request_json(self) -> dict[str, object]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if length < 0 or length > 32_768:
                raise ValueError("request body too large")
            raw = self.rfile.read(length)
            if not raw:
                return {}
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def do_POST(self) -> None:  # noqa: N802
            request = urlsplit(self.path)
            command_prefix = "/api/v1/commands/"
            if command_runner is not None and request.path.startswith(command_prefix):
                action = unquote(request.path.removeprefix(command_prefix)).strip("/")
                if not action or "/" in action:
                    self._error(404, "not_found", "local command action not found")
                    return
                if self.headers.get("X-ATS-Lab-Confirm") != "command":
                    self._error(
                        428,
                        "confirmation_required",
                        "send X-ATS-Lab-Confirm: command to confirm local action",
                    )
                    return
                try:
                    self._send_json(command_runner.run(action))
                except LocalCommandError as error:
                    self._error(400, "invalid_request", str(error))
                return
            item_prefix = "/api/v1/work-items/"
            if (
                control_service is not None
                and request.path.startswith(item_prefix)
                and request.path.endswith("/resolve")
            ):
                encoded_id = request.path.removeprefix(item_prefix)[:-len("/resolve")]
                work_item_id = unquote(encoded_id).strip("/")
                if not work_item_id or "/" in work_item_id:
                    self._error(404, "not_found", "work item not found")
                    return
                if self.headers.get("X-ATS-Lab-Confirm") != "resolve":
                    self._error(
                        428,
                        "confirmation_required",
                        "send X-ATS-Lab-Confirm: resolve to confirm item resolution",
                    )
                    return
                try:
                    payload = self._request_json()
                    code = str(payload.get("resolution_code") or "")[:200]
                    detail = str(payload.get("detail") or "")[:4_000]
                    evidence_ids = payload.get("evidence_ids") or []
                    if not isinstance(evidence_ids, list):
                        raise ValueError("evidence_ids must be a list")
                    self._send_json(control_service.resolve_work_item(
                        work_item_id,
                        resolution_code=code,
                        detail=detail,
                        evidence_ids=[str(item)[:200] for item in evidence_ids[:50]],
                    ))
                except (KeyError, OSError, sqlite3.Error) as error:
                    self._error(503, "resolution_unavailable", str(error))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    self._error(400, "invalid_request", str(error))
                return
            prefix = "/api/v1/control/"
            if control_service is None or not request.path.startswith(prefix):
                self._reject_mutation()
                return
            action = request.path.removeprefix(prefix).strip("/")
            if action not in ControlService.ACTIONS:
                self._error(404, "not_found", "control action not found")
                return
            if self.headers.get("X-ATS-Lab-Confirm") != action:
                self._error(
                    428,
                    "confirmation_required",
                    f"send X-ATS-Lab-Confirm: {action} to confirm control action",
                )
                return
            try:
                self._send_json(control_service.apply(action))
            except (KeyError, OSError, sqlite3.Error) as error:
                self._error(503, "control_unavailable", str(error))
            except ValueError as error:
                self._error(400, "invalid_request", str(error))

        def do_PUT(self) -> None:  # noqa: N802
            self._reject_mutation()

        def do_PATCH(self) -> None:  # noqa: N802
            self._reject_mutation()

        def do_DELETE(self) -> None:  # noqa: N802
            self._reject_mutation()

    return WebApiHandler


def serve(
    database: WorkflowDatabase,
    host: str = "127.0.0.1",
    port: int = 8766,
    *,
    claim_timeout_seconds: int = 7200,
) -> None:
    """Serve the read-only backend API for CLI and web clients.

    The backend deliberately owns no lifecycle mutations yet. It exposes a
    loopback-first JSON surface over the canonical SQLite projections; the
    existing dashboard remains a separate presentation frontend.
    """
    database.initialize()
    api = ReadOnlyApi(database, claim_timeout_seconds=claim_timeout_seconds)
    server = ThreadingHTTPServer((host, port), make_handler(api))
    print(f"ATS Lab backend API: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def serve_web(
    database: WorkflowDatabase,
    repo: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    claim_timeout_seconds: int = 7200,
) -> None:
    """Serve the static Control Room and read-only API from one origin."""
    frontend_dir = repo.resolve() / "frontend"
    if not frontend_dir.is_dir():
        raise FileNotFoundError(f"frontend directory missing: {frontend_dir}")
    database.initialize()
    api = ReadOnlyApi(database, claim_timeout_seconds=claim_timeout_seconds)
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    control_service = (
        ControlService(database, repo) if host in loopback_hosts else None
    )
    command_runner = (
        LocalCommandRunner(repo) if host in loopback_hosts else None
    )
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(
            api,
            static_dir=frontend_dir,
            control_service=control_service,
            command_runner=command_runner,
        ),
    )
    print(f"ATS Lab Control Room: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
