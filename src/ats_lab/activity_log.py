"""Human-first activity stream for the attached ATS Lab operator CLI."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TextIO
from urllib.parse import urlsplit


_ANSI_RESET = "\033[0m"
_ANSI = {
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "magenta": "\033[35m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}
_OSC8 = re.compile(r"\033\]8;;.*?\007|\033\]8;;\007")
_ANSI_CODES = re.compile(r"\033\[[0-?]*[ -/]*[@-~]")

DISPLAY_EVENT_TYPES = frozenset({
    "research_started",
    "research_attached",
    "stop_requested",
    "preflight_completed",
    "synthesis_started",
    "synthesis_completed",
    "synthesis_failed",
    "run_started",
    "run_completed",
    "run_failed",
    "analysis_started",
    "analysis_completed",
    "analysis_failed",
    "attention",
})

_STAGE_LABELS = {
    "starting": "STARTING",
    "synthesizing": "SYNTHESIS",
    "executing": "RUNNING",
    "analyzing": "ANALYSIS",
    "hpo_analysis": "ANALYSIS",
    "infrastructure_blocked": "BLOCKED",
    "paused": "PAUSED",
    "stopping": "STOPPING",
    "stopped": "STOPPED",
    "idle": "WAITING",
}
_OPERATION_LABELS = {
    "backtest": "RUNNING",
    "significance": "RULE TEST",
    "monte_carlo": "MONTE CARLO",
    "hpo": "HPO",
}
_CHECK_LABELS = {
    "docker_daemon": "Docker",
    "jesse_dashboard": "Jesse",
    "jesse_mcp": "Jesse MCP",
    "memory_api": "Memory",
    "postgres": "Postgres",
}
_VERDICT_LABELS = {
    "pass": "PASS",
    "inconclusive": "INCONCLUSIVE",
    "revise": "REVISE",
    "reject": "REJECT",
    "hpo_candidate": "HPO CANDIDATE",
    "paper_trade_candidate": "PAPER CANDIDATE",
    "infrastructure_failure": "INFRASTRUCTURE",
}


def _paint(value: object, style: str, enabled: bool) -> str:
    text = str(value)
    if not enabled or style not in _ANSI:
        return text
    return f"{_ANSI[style]}{text}{_ANSI_RESET}"


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_total_duration(seconds: float | int) -> str:
    total_minutes = max(0, int(float(seconds)) // 60)
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} hrs : {minutes:02d} min"


def format_event_gap(seconds: float | int) -> str:
    value = max(0, int(float(seconds)))
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} hr" if hours == 1 else f"{hours} hrs")
    if minutes or hours:
        parts.append(f"{minutes} min")
    if seconds or not parts:
        parts.append(f"{seconds} sec")
    return "+" + " ".join(parts)


def _compact(value: object, limit: int = 140) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _safe_url(value: object) -> str | None:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    return None


def terminal_link(label: str, url: object, *, enabled: bool) -> str:
    target = _safe_url(url)
    if not target:
        return label
    if not enabled:
        return f"{label}: {target}"
    return f"\033]8;;{target}\007{label} ↗\033]8;;\007"


def strip_terminal_controls(value: str) -> str:
    return _ANSI_CODES.sub("", _OSC8.sub("", value))


@dataclass(frozen=True)
class ActivityLogConfig:
    """Human log-file settings loaded from ``[logging]``."""

    log_to_file: bool = True
    log_dir: str = ".ats-lab/logs/{date}_log"
    repo: Path = Path.cwd()

    @classmethod
    def from_file(cls, path: Path, *, repo: Path) -> "ActivityLogConfig":
        if not path.is_file():
            return cls(repo=repo)
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
        section = payload.get("logging", {})
        if not isinstance(section, dict):
            raise ValueError("logging config must be a table")
        enabled = section.get("log_to_file", True)
        if not isinstance(enabled, bool):
            raise ValueError("logging.log_to_file must be true or false")
        log_dir = section.get("log_dir", ".ats-lab/logs/{date}_log")
        if not isinstance(log_dir, str) or not log_dir.strip():
            raise ValueError("logging.log_dir must be a non-empty string")
        return cls(
            log_to_file=enabled,
            log_dir=log_dir.replace("{ats-lab}", ".ats-lab"),
            repo=repo,
        )

    def daily_path(self, when: datetime | None = None) -> Path:
        local = (when or _utc_now()).astimezone()
        date_text = local.date().isoformat()
        value = self.log_dir
        if "{date}" in value:
            rendered = value.replace("{date}", date_text)
        else:
            rendered = str(Path(value) / f"{date_text}_log")
        path = Path(rendered).expanduser()
        return path if path.is_absolute() else self.repo / path


def load_activity_log_config(repo: Path) -> ActivityLogConfig:
    return ActivityLogConfig.from_file(
        repo / ".ats-lab" / "config.toml", repo=repo,
    )


class ActivityFileWriter:
    def __init__(self, config: ActivityLogConfig) -> None:
        self.config = config

    def write(self, lines: Iterable[str], *, when: datetime | None = None) -> None:
        if not self.config.log_to_file:
            return
        path = self.config.daily_path(when)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(strip_terminal_controls(str(line)) + "\n")
        except OSError:
            # Operator display must continue if optional file logging fails.
            return


@dataclass(frozen=True)
class ActivityEvent:
    id: int
    aggregate_type: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    occurred_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "ActivityEvent":
        raw_payload = row.get("payload_json", row.get("payload", "{}"))
        try:
            payload = json.loads(raw_payload or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return cls(
            id=int(row["id"]),
            aggregate_type=str(row.get("aggregate_type") or ""),
            aggregate_id=str(row.get("aggregate_id") or ""),
            event_type=str(row.get("event_type") or ""),
            payload=payload,
            occurred_at=str(row.get("occurred_at") or ""),
        )


def is_display_event(event: ActivityEvent) -> bool:
    return event.event_type in DISPLAY_EVENT_TYPES


def _stage_for_operation(operation: object) -> str:
    return _OPERATION_LABELS.get(str(operation or "backtest"), "RUNNING")


def _stage_for_event(event: ActivityEvent) -> str:
    payload_stage = event.payload.get("stage") or event.payload.get("phase")
    if payload_stage:
        return _STAGE_LABELS.get(str(payload_stage), str(payload_stage).upper())
    operation = event.payload.get("operation")
    if operation:
        return _stage_for_operation(operation)
    return {
        "research_started": "STARTING",
        "research_attached": "STARTING",
        "stop_requested": "STOPPING",
        "preflight_completed": "PREFLIGHT",
        "synthesis_started": "SYNTHESIS",
        "synthesis_completed": "SYNTHESIS",
        "synthesis_failed": "SYNTHESIS",
        "run_started": "RUNNING",
        "run_completed": "RUNNING",
        "run_failed": "ATTENTION",
        "analysis_started": "ANALYSIS",
        "analysis_completed": "ANALYSIS",
        "analysis_failed": "ATTENTION",
        "attention": "ATTENTION",
    }.get(event.event_type, "WAITING")


def _tokens_for_event(event: ActivityEvent) -> int | None:
    for key in ("tokens_since_event", "token_count", "tokens"):
        value = event.payload.get(key)
        if isinstance(value, Mapping):
            value = value.get("total") or value.get("total_tokens")
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            continue
    usage = event.payload.get("usage")
    if isinstance(usage, Mapping):
        value = usage.get("total_tokens")
        if value is None:
            value = sum(
                int(usage.get(key) or 0)
                for key in ("input_tokens", "output_tokens")
                if str(usage.get(key) or "").replace(".", "", 1).isdigit()
            )
        try:
            return max(0, int(value)) if value is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _route_text(payload: Mapping[str, Any]) -> str | None:
    route = payload.get("route")
    if not isinstance(route, Mapping):
        routes = payload.get("routes")
        route = routes[0] if isinstance(routes, list) and routes else None
    if not isinstance(route, Mapping):
        return None
    values = [
        route.get("symbol"), route.get("timeframe"),
        f"{route.get('start_date')} → {route.get('finish_date')}"
        if route.get("start_date") or route.get("finish_date") else None,
    ]
    return " · ".join(str(value) for value in values if value)


def _description(payload: Mapping[str, Any]) -> str:
    for key in (
        "description", "hypothesis", "thesis", "entry_rule_summary",
        "why_this_now", "summary", "finding",
    ):
        value = payload.get(key)
        if value:
            return _compact(value)
    return ""


def _metric_state(payload: Mapping[str, Any], name: str) -> str:
    states = payload.get("metric_states")
    if isinstance(states, Mapping) and states.get(name) in {"green", "yellow", "red"}:
        return str(states[name])
    return ""


def _metric(payload: Mapping[str, Any], name: str, label: str, suffix: str = "") -> str:
    value = payload.get("metrics", {}).get(name) if isinstance(payload.get("metrics"), Mapping) else None
    if value in (None, ""):
        return f"{label}=—"
    try:
        number = float(value)
        if name == "trade_count" and number.is_integer():
            text = f"{int(number)}{suffix}"
        else:
            text = f"{number:+.2f}{suffix}" if name == "net_profit_percentage" else f"{number:.2f}{suffix}"
    except (TypeError, ValueError):
        text = f"{value}{suffix}"
    return f"{label}={text}"


def _metric_line(payload: Mapping[str, Any], *, color: bool) -> str:
    parts = []
    for name, label, suffix in (
        ("trade_count", "trades", ""),
        ("net_profit_percentage", "net", "%"),
        ("sharpe_ratio", "sharpe", ""),
        ("max_drawdown_percentage", "max_dd", "%"),
    ):
        value = _metric(payload, name, label, suffix)
        state = _metric_state(payload, name)
        parts.append(_paint(value, state, color) if state else value)
    return " · ".join(parts)


def _event_lines(event: ActivityEvent, *, color: bool, links: bool) -> list[str]:
    payload = event.payload
    if event.event_type == "research_started":
        return [_paint("STARTING:", "cyan", color) + " Starting ATS Research Lab"]
    if event.event_type == "research_attached":
        return [_paint("STARTING:", "cyan", color) + " ATS Lab already running; attached"]
    if event.event_type == "stop_requested":
        return [
            _paint("STOPPING:", "yellow", color)
            + " Graceful stop requested · finishing current work"
        ]
    if event.event_type == "preflight_completed":
        checks = []
        for check in payload.get("checks", []):
            if not isinstance(check, Mapping):
                continue
            name = _CHECK_LABELS.get(str(check.get("name")), str(check.get("name") or "Check"))
            status = str(check.get("status") or "unknown")
            word = {"healthy": "OK", "ok": "OK", "degraded": "DEGRADED"}.get(status, "FAILED")
            style = "green" if word == "OK" else "yellow" if word == "DEGRADED" else "red"
            checks.append(f"{name} {_paint(word, style, color)}")
        return [_paint("PREFLIGHT:", "cyan", color) + " " + " · ".join(checks)]
    if event.event_type == "synthesis_started":
        requested = payload.get("requested", 0)
        return [_paint("SYNTHESIS:", "cyan", color) + f" Synthesising {requested} new tests"]
    if event.event_type == "synthesis_failed":
        return [_paint("SYNTHESIS:", "red", color) + f" Failed · {_compact(payload.get('detail') or 'unknown error')}"]
    if event.event_type == "synthesis_completed":
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        lines = [_paint("SYNTHESIS:", "cyan", color) + f" Created {len(items)} new tests"]
        for index, item in enumerate(items, start=1):
            if not isinstance(item, Mapping):
                continue
            lane = "NEW" if item.get("lane") in {"new_concept", "new"} else "IMPROVE"
            route = _route_text(item)
            suffix = f" · {route}" if route else ""
            lines.append(f"  => {index:02d}  {_paint(lane, 'cyan', color)}  {item.get('strategy') or item.get('strategy_name') or 'unknown'}{suffix}")
            description = _description(item)
            if description:
                lines.append(f"      ↳ evaluating: {_paint(description, 'dim', color)}")
        return lines
    if event.event_type in {"run_started", "run_completed", "run_failed"}:
        stage = _stage_for_operation(payload.get("operation"))
        operation = str(payload.get("operation") or "backtest")
        action = {
            "run_started": "HPO started" if operation == "hpo" else "Backtest started",
            "run_completed": "HPO Complete" if operation == "hpo" else "Backtest Complete",
            "run_failed": "Failed",
        }[event.event_type]
        completed = payload.get("completed", 0)
        total = payload.get("total", 0)
        count = f" ({completed}/{total})" if total else ""
        style = "green" if event.event_type == "run_completed" else "red" if event.event_type == "run_failed" else "blue"
        lines = [_paint(f"{stage}{count}:", style, color) + f" {action} · {payload.get('strategy') or payload.get('strategy_name') or 'unknown'}"]
        route = _route_text(payload)
        if route:
            lines.append(f"  {route}")
        if event.event_type == "run_completed":
            lines.append("  " + _metric_line(payload, color=color))
            link = terminal_link("Jesse", payload.get("dashboard_url"), enabled=links)
            if link != "Jesse":
                lines.append("  " + _paint(link, "blue", color))
        elif event.event_type == "run_failed":
            lines.append(f"  {_paint(str(payload.get('blocker_code') or 'execution failure'), 'red', color)} · {_compact(payload.get('detail') or '')}")
        description = _description(payload) if event.event_type == "run_started" else ""
        if description:
            lines.append(f"  ↳ evaluating: {_paint(description, 'dim', color)}")
        return lines
    if event.event_type == "analysis_started":
        count = payload.get("count")
        suffix = f" ({count} items)" if count is not None else ""
        return [_paint("ANALYSIS:", "cyan", color) + f" Starting{suffix}"]
    if event.event_type == "analysis_completed":
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        total = payload.get("total", len(items))
        lines = [_paint("ANALYSIS:", "green", color) + f" Completed ({len(items)}/{total})"]
        for index, item in enumerate(items, start=1):
            if not isinstance(item, Mapping):
                continue
            verdict = str(item.get("verdict") or "inconclusive")
            label = _VERDICT_LABELS.get(verdict, verdict.upper())
            style = {"pass": "green", "revise": "yellow", "inconclusive": "yellow", "reject": "red"}.get(verdict, "cyan")
            summary = _compact(item.get("summary") or item.get("finding") or "")
            detail = f" · {summary}" if summary else ""
            lines.append(f"  {index:02d}  {_paint(label, style, color)}  {item.get('strategy') or item.get('experiment_id') or 'unknown'}{detail}")
        next_action = payload.get("next_action")
        if next_action:
            lines.append(f"  next: {_compact(next_action)}")
        return lines
    if event.event_type == "analysis_failed":
        return [_paint("ANALYSIS:", "red", color) + f" Failed · {_compact(payload.get('detail') or 'unknown error')}"]
    if event.event_type == "attention":
        return [_paint("ATTENTION:", "red", color) + f" {_compact(payload.get('detail') or 'operator attention required')}"]
    return []


def render_activity_event(event: ActivityEvent, *, color: bool = False, links: bool = True) -> str:
    return "\n".join(_event_lines(event, color=color, links=links))


def render_footer(
    stage: str,
    *,
    total_seconds: float,
    since_event_seconds: float,
    tokens: int | None = None,
    color: bool = False,
    width: int | None = None,
) -> str:
    stage_text = _paint(f"{stage:<16}", "cyan", color)
    text = f"└─ {stage_text} · {format_total_duration(total_seconds)} ({format_event_gap(since_event_seconds)})"
    if tokens is not None:
        text += f" · {_paint(f'(^ {tokens:,})', 'magenta', color)}"
    if width and len(strip_terminal_controls(text)) > width:
        plain = strip_terminal_controls(text)
        text = plain[: max(1, width - 1)] + "…"
    return text


class ActivityFollower:
    """Follow display-worthy durable events with a one-line live footer."""

    def __init__(
        self,
        database: Any,
        *,
        output: TextIO = sys.stdout,
        config: ActivityLogConfig | None = None,
        cursor: int = 0,
        started_at: str | None = None,
        interval: float = 1.0,
        color: bool | None = None,
        links: bool | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if interval <= 0:
            raise ValueError("activity interval must be positive")
        self.database = database
        self.output = output
        self.config = config or ActivityLogConfig(repo=Path.cwd())
        self.cursor = max(0, int(cursor))
        self.started_at = _parse_time(started_at) or clock()
        self.interval = interval
        self.color = _color_enabled(output) if color is None else color
        self.links = self.color if links is None else links
        self.sleep = sleep
        self.clock = clock
        self.stage = "STARTING"
        self.last_event_at = self.started_at
        self.tokens: int | None = None
        self.stop_requested = False
        self._footer_drawn = False
        self._file = ActivityFileWriter(self.config)

    def _write(self, text: str) -> None:
        self.output.write(text)
        self.output.flush()

    def _clear_footer(self) -> None:
        if self._footer_drawn and getattr(self.output, "isatty", lambda: False)():
            self._write("\r\033[K")
            self._footer_drawn = False

    def _footer(self, now: datetime) -> str:
        return render_footer(
            self.stage,
            total_seconds=(now - self.started_at).total_seconds(),
            since_event_seconds=(now - self.last_event_at).total_seconds(),
            tokens=self.tokens,
            color=self.color,
        )

    def _draw_footer(self, now: datetime, *, force: bool = False) -> None:
        footer = self._footer(now)
        if getattr(self.output, "isatty", lambda: False)():
            prefix = "\r\033[K" if self._footer_drawn else ""
            self._write(prefix + footer)
            self._footer_drawn = True
        elif force:
            self._write(footer + "\n")

    def _consume(self, rows: Iterable[Mapping[str, Any]], now: datetime) -> int:
        count = 0
        for row in rows:
            self.cursor = max(self.cursor, int(row["id"]))
            event = ActivityEvent.from_row(row)
            if not is_display_event(event):
                continue
            self._clear_footer()
            rendered = render_activity_event(
                event, color=self.color, links=self.links,
            )
            if rendered:
                self._write(rendered + "\n")
                self._file.write(
                    render_activity_event(event, color=False, links=False).splitlines(),
                    when=_parse_time(event.occurred_at) or now,
                )
            self.stage = _stage_for_event(event)
            self.tokens = _tokens_for_event(event)
            event_time = _parse_time(event.occurred_at) or now
            if event.event_type == "research_started":
                self.started_at = event_time
            if event.event_type == "stop_requested":
                self.stop_requested = True
            self.last_event_at = event_time
            count += 1
        self._draw_footer(now, force=count > 0)
        return count

    def run(self, *, max_iterations: int | None = None) -> int:
        iterations = 0
        while True:
            now = self.clock()
            rows = self.database.events_after(self.cursor, limit=100)
            self._consume(rows, now)
            runtime = self.database.supervisor_runtime_status() or {}
            phase = str(runtime.get("phase") or "")
            control_reader = getattr(self.database, "control_status", None)
            control = (control_reader() or {}) if control_reader is not None else {}
            stop_pending = bool(
                self.stop_requested
                or phase == "stop_requested"
                or control.get("desired_state") == "stop_requested"
            )
            runtime_stage = _STAGE_LABELS.get(phase)
            if stop_pending:
                self.stage = "STOPPING"
            elif runtime_stage and phase not in {"stopped", "stop_requested"}:
                self.stage = runtime_stage
            if stop_pending and phase != "stopped":
                self._draw_footer(now)
            if phase == "stopped":
                self.stage = "STOPPED"
                self._draw_footer(now, force=True)
                break
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                break
            self.sleep(self.interval)
        if self._footer_drawn:
            self._write("\n")
            self._footer_drawn = False
        return self.cursor


def _color_enabled(output: TextIO) -> bool:
    return bool(getattr(output, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")
