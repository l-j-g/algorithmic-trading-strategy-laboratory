"""Human-readable terminal monitor and operator console."""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, TextIO

from .database import WorkflowDatabase
from .status import operator_status


_CANDIDATE_VERDICTS = {
    "paper_trade_candidate", "hpo_candidate", "revise",
}
_SPLIT_PRIORITY = {
    "oos": 0, "holdout": 1, "rolling": 2, "train": 3, None: 4,
}


def distinct_candidate_evidence(rows: Iterable[object]) -> list[object]:
    """One representative canonical evidence record per experiment."""
    selected: dict[str, object] = {}
    for item in rows:
        row = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        if row.get("verdict") not in _CANDIDATE_VERDICTS:
            continue
        experiment_id = str(row.get("experiment_id") or "")
        if not experiment_id:
            continue
        current = selected.get(experiment_id)
        current_row = (
            current.to_dict() if hasattr(current, "to_dict") else dict(current)
        ) if current is not None else None
        rank = (
            _SPLIT_PRIORITY.get(row.get("evidence_split"), 5),
            -(1 if row.get("completed_at") else 0),
        )
        current_rank = (
            _SPLIT_PRIORITY.get(current_row.get("evidence_split"), 5),
            -(1 if current_row.get("completed_at") else 0),
        ) if current_row is not None else None
        if current_rank is None or rank < current_rank:
            selected[experiment_id] = item
    return list(selected.values())


def monitor_snapshot(database: WorkflowDatabase) -> dict:
    status = operator_status(database)
    status["control"] = database.control_status()
    status["supervisor"] = database.supervisor_runtime_status()
    status["active_items"] = database.rows(
        """SELECT id,strategy,priority,state,attempts,blocker_code
           FROM active_queue
           ORDER BY CASE state
               WHEN 'running' THEN 0 WHEN 'ready' THEN 1
               WHEN 'waiting_retry' THEN 2 WHEN 'blocked' THEN 3 ELSE 4 END,
               priority,created_at,id
           LIMIT 12"""
    )
    status["recent_events"] = database.rows(
        """SELECT event_type,aggregate_id,occurred_at
           FROM events ORDER BY id DESC LIMIT 5"""
    )
    candidates = [
        item.to_dict()
        for item in distinct_candidate_evidence(
            database.query_normalized_evidence(limit=5000)
        )
    ]
    candidates.sort(
        key=lambda row: {
            "paper_trade_candidate": 0,
            "hpo_candidate": 1,
            "revise": 2,
        }.get(row.get("verdict"), 3)
    )
    status["candidates"] = candidates[:5]
    return status


def _age(timestamp: str | None) -> str:
    if not timestamp:
        return "-"
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return timestamp
    seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _duration(value: object) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(float(value)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _cell(value: object, width: int) -> str:
    text = "—" if value in (None, "") else str(value)
    if len(text) > width:
        text = text[: max(1, width - 1)] + "…"
    return text.ljust(width)


def render_monitor(snapshot: dict) -> str:
    control = snapshot["control"]
    runtime = snapshot.get("supervisor")
    synthesis = snapshot["synthesis"]
    hpo = snapshot.get("hpo", {})
    hpo_counts = hpo.get("counts", {})
    analyzer = hpo.get("analyzer")
    recent_timing = next(iter(hpo.get("recent_timings", [])), None)
    cohort = synthesis.get("latest_cohort")
    states = snapshot["work_states"]
    health = "HEALTHY" if snapshot["healthy"] else "ATTENTION"
    lines = [
        f"ATS LAB  {snapshot['checked_at']}",
        (
            f"CONTROL {control['desired_state']}  "
            f"SUPERVISOR {runtime['phase'] if runtime else 'not_reported'}  "
            f"{health}"
        ),
    ]
    if runtime:
        batch = f"  batch={runtime['batch_id']}" if runtime.get("batch_id") else ""
        lines.append(
            f"worker={runtime['worker_id']} pid={runtime['process_id']} "
            f"heartbeat={_age(runtime['heartbeat_at'])} ago{batch}"
        )
    lines.extend([
        "",
        "QUEUE  "
        + "  ".join(
            f"{state}={states.get(state, 0)}"
            for state in (
                "scheduled", "ready", "running", "waiting_retry", "blocked", "finished"
            )
        ),
        (
            f"BATCH  executing={snapshot['running_execution_claims']}  "
            f"awaiting_analysis={snapshot['awaiting_batch_evaluation']}  "
            f"stale={snapshot['unresolved_execution_claims']}"
        ),
        (
            f"COHORT remaining={synthesis['remaining_chains']}  "
            + (
                f"{cohort['id']} {cohort['status']} "
                f"{cohort['generated_count']}/{cohort['requested_count']}"
                if cohort else "none"
            )
        ),
        (
            "HPO    "
            + "  ".join(
                f"{state}={hpo_counts.get(state, 0)}"
                for state in (
                    "hpo_candidate", "hpo_scheduled", "hpo_running",
                    "hpo_analysis", "validation", "paper_trade_candidate",
                    "revise", "reject",
                )
            )
        ),
        (
            "ANALYZER "
            + (
                f"{analyzer.get('state') or '—'} "
                f"job={analyzer.get('job_id') or '—'} "
                f"tries={analyzer.get('attempts') or 0}"
                if analyzer else "idle"
            )
        ),
        (
            "STAGE  "
            + (
                f"{recent_timing.get('stage') or '—'} "
                f"{recent_timing.get('state') or '—'} "
                f"duration={_duration(recent_timing.get('duration_seconds'))}"
                if recent_timing else "no timing evidence"
            )
        ),
        f"NEXT   {snapshot['next_action']}",
        "",
        "ACTIVE",
        (
            f"{_cell('state', 15)} {_cell('prio', 5)} {_cell('strategy', 26)} "
            f"{_cell('job', 31)} blocker"
        ),
    ])
    if snapshot["active_items"]:
        for item in snapshot["active_items"]:
            lines.append(
                f"{_cell(item['state'], 15)} {_cell(item['priority'], 5)} "
                f"{_cell(item['strategy'], 26)} {_cell(item['id'], 31)} "
                f"{item['blocker_code'] or '-'}"
            )
    else:
        lines.append("(none)")
    lines.extend(["", "CANDIDATES"])
    if snapshot["candidates"]:
        for candidate in snapshot["candidates"]:
            lines.append(
                f"{_cell(candidate['verdict'], 23)} "
                f"{_cell(candidate['strategy'], 28)} {candidate['experiment_id']}"
            )
    else:
        lines.append("(none)")
    lines.extend([
        "",
        "Commands: control pause | control resume | control stop",
        "          monitor --watch | console | queue --state blocked",
    ])
    return "\n".join(lines)


def watch_monitor(
    database: WorkflowDatabase,
    *,
    interval: float = 5,
    output: TextIO = sys.stdout,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    while True:
        if getattr(output, "isatty", lambda: False)():
            output.write("\033[2J\033[H")
        output.write(render_monitor(monitor_snapshot(database)) + "\n")
        output.flush()
        sleep(interval)


def render_table(
    rows: Iterable[Mapping[str, object]],
    columns: tuple[tuple[str, str, int], ...],
) -> str:
    values = list(rows)
    if not values:
        return "(none)"
    header = " ".join(_cell(label, width) for _, label, width in columns).rstrip()
    rule = " ".join("-" * width for _, _, width in columns).rstrip()
    body = [
        " ".join(_cell(row.get(key), width) for key, _, width in columns).rstrip()
        for row in values
    ]
    return "\n".join((header, rule, *body))


def render_evidence(rows: Iterable[object]) -> str:
    values = [
        item.to_dict() if hasattr(item, "to_dict") else dict(item)
        for item in rows
    ]
    return render_table(values, (
        ("strategy", "strategy", 24),
        ("lifecycle_stage", "stage", 14),
        ("verdict", "verdict", 22),
        ("symbol", "symbol", 12),
        ("timeframe", "tf", 5),
        ("evidence_split", "split", 8),
        ("net_profit_percentage", "net %", 9),
        ("max_drawdown_percentage", "dd %", 9),
        ("sharpe_ratio", "Sharpe", 8),
        ("trade_count", "trades", 7),
        ("next_action", "next action", 28),
    ))


def render_hpo_studies(rows: Iterable[Mapping[str, object]]) -> str:
    return render_table(rows, (
        ("lifecycle_state", "lifecycle", 23),
        ("strategy", "strategy", 24),
        ("study_id", "study", 24),
        ("objective_name", "objective", 15),
        ("completed_trial_count", "done", 6),
        ("trial_count", "trials", 7),
        ("selected_trial_count", "selected", 9),
        ("validation_count", "valid.", 7),
        ("disposition", "disposition", 18),
        ("next_action", "next action", 28),
    ))


def render_stage_timings(rows: Iterable[Mapping[str, object]]) -> str:
    values = []
    for row in rows:
        item = dict(row)
        item["duration"] = _duration(item.get("duration_seconds"))
        values.append(item)
    return render_table(values, (
        ("work_item_id", "job", 28),
        ("stage", "stage", 18),
        ("attempt", "try", 4),
        ("state", "state", 12),
        ("duration", "duration", 10),
        ("outcome", "outcome", 18),
        ("started_at", "started", 24),
    ))


def render_analyzer(analyzer: Mapping[str, object] | None) -> str:
    if not analyzer:
        return "ANALYZER idle"
    return render_table((analyzer,), (
        ("state", "state", 14),
        ("job_id", "job", 26),
        ("study_id", "study", 24),
        ("attempts", "tries", 6),
        ("claimed_by", "worker", 22),
        ("retry_after", "retry after", 24),
        ("last_error", "last error", 34),
    ))


def render_hpo_detail(detail: Mapping[str, object] | None) -> str:
    if not detail:
        return "(not found)"
    study = detail.get("study")
    study_row = study if isinstance(study, Mapping) else detail
    sections = ["STUDY", render_hpo_studies((study_row,))]
    trials = detail.get("selected_trials")
    if isinstance(trials, list):
        sections.extend([
            "",
            "SELECTED TRIALS",
            render_table(trials, (
                ("rank", "rank", 5),
                ("trial_number", "trial", 7),
                ("objective_value", "objective", 12),
                ("classification", "class", 16),
                ("run_id", "run", 24),
                ("session_id", "session", 24),
                ("selection_reason", "reason", 30),
            )),
        ])
    validations = detail.get("validations")
    if isinstance(validations, list):
        validation_rows = []
        for row in validations:
            item = dict(row)
            item["status"] = (
                item.get("status")
                or item.get("validation_status")
                or item.get("state")
            )
            item["display_detail"] = (
                item.get("blocker_detail") or item.get("finding")
            )
            validation_rows.append(item)
        sections.extend([
            "",
            "VALIDATION",
            render_table(validation_rows, (
                ("status", "status", 14),
                ("readiness_status", "readiness", 20),
                ("experiment_id", "experiment", 24),
                ("run_id", "run", 24),
                ("session_id", "session", 24),
                ("display_detail", "blocker / finding", 34),
            )),
        ])
    timings = detail.get("timings")
    if isinstance(timings, list):
        sections.extend(["", "TIMINGS", render_stage_timings(timings)])
    analyzer = detail.get("analysis_job")
    if isinstance(analyzer, Mapping):
        sections.extend(["", "ANALYZER", render_analyzer(analyzer)])
    return "\n".join(sections)


def render_control(control: Mapping[str, object], supervisor: Mapping[str, object] | None = None) -> str:
    phase = supervisor.get("phase") if supervisor else None
    return (
        f"CONTROL {_cell(control.get('desired_state'), 14).rstrip()}  "
        f"SUPERVISOR {_cell(phase, 14).rstrip()}  "
        f"updated={_cell(control.get('updated_at'), 24).rstrip()}"
    )


def run_console(
    database: WorkflowDatabase,
    *,
    interval: float = 5,
    input_stream: TextIO = sys.stdin,
    output: TextIO = sys.stdout,
) -> int:
    """Run small interactive supervisor console."""
    output.write(render_monitor(monitor_snapshot(database)) + "\n")
    while True:
        output.write("\nats> ")
        output.flush()
        line = input_stream.readline()
        if not line:
            output.write("\n")
            return 0
        parts = line.strip().lower().split()
        if not parts or parts[0] in {"status", "refresh"}:
            output.write(render_monitor(monitor_snapshot(database)) + "\n")
        elif parts[0] in {"quit", "exit"}:
            return 0
        elif parts[0] in {"pause", "resume", "stop"}:
            target = {
                "pause": "paused",
                "resume": "running",
                "stop": "stop_requested",
            }[parts[0]]
            state = database.set_control_state(target, updated_by="console")
            output.write(
                render_control(state, database.supervisor_runtime_status()) + "\n"
            )
        elif parts[0] == "watch":
            seconds = float(parts[1]) if len(parts) > 1 else interval
            try:
                watch_monitor(database, interval=seconds, output=output)
            except KeyboardInterrupt:
                output.write("\nwatch stopped; console active\n")
        elif parts[0] == "queue":
            parameters: tuple = ()
            query = "SELECT * FROM active_queue"
            if len(parts) > 1:
                query += " WHERE state=?"
                parameters = (parts[1],)
            query += " ORDER BY priority,created_at,id"
            output.write(render_table(
                database.rows(query, parameters),
                (
                    ("state", "state", 15),
                    ("priority", "prio", 5),
                    ("strategy", "strategy", 26),
                    ("id", "job", 31),
                    ("blocker_code", "blocker", 24),
                ),
            ) + "\n")
        elif parts[0] == "candidates":
            evidence = distinct_candidate_evidence(
                database.query_normalized_evidence(limit=5000)
            )[:20]
            output.write(render_evidence(evidence) + "\n")
        elif parts[0] == "evidence":
            output.write(
                render_evidence(database.query_normalized_evidence(limit=20))
                + "\n"
            )
        elif parts[0] == "hpo":
            query = getattr(database, "hpo_studies", None)
            output.write(
                render_hpo_studies(query(limit=20) if query else []) + "\n"
            )
        elif parts[0] == "analyzer":
            query = getattr(database, "current_analyzer_status", None)
            output.write(render_analyzer(query() if query else None) + "\n")
        elif parts[0] == "timings":
            query = getattr(database, "work_item_stage_timings", None)
            output.write(
                render_stage_timings(query(limit=20) if query else []) + "\n"
            )
        elif parts[0] == "help":
            output.write(
                "status | refresh | watch [seconds] | queue [state]\n"
                "candidates | evidence | hpo | analyzer | timings\n"
                "pause | resume | stop | help | quit\n"
            )
        else:
            output.write(f"unknown command: {' '.join(parts)}; use help\n")
