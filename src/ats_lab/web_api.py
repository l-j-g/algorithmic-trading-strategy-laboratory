"""Typed, read-only backend snapshots for CLI and local web consumers."""
from __future__ import annotations

import json
import mimetypes
import sqlite3
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .dashboard import hpo_detail_snapshot, query_page
from .database import WorkflowDatabase
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

        def do_POST(self) -> None:  # noqa: N802
            request = urlsplit(self.path)
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
    server = ThreadingHTTPServer(
        (host, port),
        make_handler(
            api,
            static_dir=frontend_dir,
            control_service=control_service,
        ),
    )
    print(f"ATS Lab Control Room: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
