"""Responsive semantic rendering for the ATS Lab terminal UI."""
from __future__ import annotations

import curses
from typing import Any, Protocol

from .tui_types import (
    MEMORY_STATE_ROLES,
    Role,
    STATE_ROLES,
    TuiLine,
    TuiState,
    View,
)


class ScreenRenderer(Protocol):
    def render(
        self, model: dict[str, Any], state: TuiState, *, width: int, height: int,
    ) -> list[TuiLine]: ...

    def attributes(self) -> dict[Role, int]: ...

    def draw(
        self, screen: Any, lines: list[TuiLine], attrs: dict[Role, int],
    ) -> None: ...


def _clip(value: object, width: int) -> str:
    text = "—" if value in (None, "") else " ".join(str(value).split())
    if width <= 0:
        return ""
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def _columns(values: list[tuple[object, int]], width: int) -> str:
    parts: list[str] = []
    remaining = width
    for index, (value, requested) in enumerate(values):
        gap = 1 if index else 0
        available = max(0, remaining - gap)
        cell_width = min(requested, available)
        if cell_width <= 0:
            break
        parts.append((" " if gap else "") + _clip(value, cell_width).ljust(cell_width))
        remaining -= gap + cell_width
    return "".join(parts).rstrip()


def _selected_window(
    rows: list[dict[str, Any]], state: TuiState, available: int,
) -> tuple[list[tuple[int, dict[str, Any]]], int]:
    if not rows or available <= 0:
        state.selected = 0
        state.scroll = 0
        return [], 0
    state.selected = max(0, min(state.selected, len(rows) - 1))
    if state.selected < state.scroll:
        state.scroll = state.selected
    if state.selected >= state.scroll + available:
        state.scroll = state.selected - available + 1
    state.scroll = max(0, min(state.scroll, max(0, len(rows) - available)))
    return list(enumerate(
        rows[state.scroll:state.scroll + available], start=state.scroll,
    )), state.selected


def _header(model: dict[str, Any], state: TuiState, width: int) -> list[TuiLine]:
    snapshot = model["snapshot"]
    runtime = snapshot.get("supervisor") or {}
    control = snapshot.get("control") or {}
    health = "HEALTHY" if snapshot.get("healthy") else "ATTENTION"
    role = Role.HEALTHY if snapshot.get("healthy") else Role.ERROR
    title = _columns([
        ("ATS LAB", 10),
        (health, 12),
        (f"supervisor:{runtime.get('phase') or 'not_reported'}", 24),
        (f"control:{control.get('desired_state') or 'running'}", 22),
        (snapshot.get("checked_at"), max(0, width - 71)),
    ], width)
    tabs = "  ".join(
        f"[{view.value + 1} {view.label}]" if view == state.view
        else f" {view.value + 1} {view.label} "
        for view in View
    )
    return [TuiLine(title, role), TuiLine(_clip(tabs, width), Role.TABS)]


def _overview(
    model: dict[str, Any], _state: TuiState, width: int, height: int,
) -> list[TuiLine]:
    snapshot = model["snapshot"]
    states = snapshot.get("work_states") or {}
    memory = model["memory"]
    guidance = model["guidance"]
    lines = [
        TuiLine("OVERVIEW", Role.SECTION),
        TuiLine(_columns([
            (f"READY {states.get('ready', 0)}", 14),
            (f"RUNNING {states.get('running', 0)}", 16),
            (f"RETRY {states.get('waiting_retry', 0)}", 14),
            (f"SCHEDULED {states.get('scheduled', 0)}", 18),
            (f"BLOCKED {states.get('blocked', 0)}", 16),
            (f"FINISHED {states.get('finished', 0)}", 18),
        ], width), Role.METRICS),
        TuiLine(
            f"MEMORY delivered={memory.get('delivered', 0)}  "
            f"pending={memory.get('pending', 0)}  retry={memory.get('retry', 0)}",
            Role.HEALTHY if not memory.get("pending") and not memory.get("retry") else Role.WARNING,
        ),
        TuiLine(""),
        TuiLine(f"NEXT  {guidance['action']}", Role.WARNING),
        TuiLine(f"WHY  {guidance['reason']}", Role.NORMAL),
        TuiLine(f"RUN   {guidance['command']}", Role.COMMAND),
        TuiLine(""),
        TuiLine("ACTIVE WORK", Role.SECTION),
    ]
    for row in (snapshot.get("active_items") or [])[:max(1, height - len(lines) - 2)]:
        lines.append(TuiLine(_columns([
            (row.get("state"), 15), (row.get("priority"), 5),
            (row.get("strategy"), 28), (row.get("id"), max(20, width - 50)),
        ], width), STATE_ROLES.get(str(row.get("state")), Role.NORMAL)))
    if not snapshot.get("active_items"):
        lines.append(TuiLine("No active work.", Role.MUTED))
    return lines


def _queue(model: dict[str, Any], state: TuiState, width: int, height: int) -> list[TuiLine]:
    rows = model["queue"]
    detail_height = 5 if state.show_detail and rows else 0
    available = max(1, height - 3 - detail_height)
    visible, selected = _selected_window(rows, state, available)
    lines = [
        TuiLine(f"QUEUE  {len(rows)} unresolved jobs", Role.SECTION),
        TuiLine(_columns([
            ("STATE", 15), ("PRI", 5), ("STRATEGY", 28),
            ("JOB", max(18, width - 75)), ("TRIES", 6), ("BLOCKER", 22),
        ], width), Role.TABLE_HEADER),
    ]
    for index, row in visible:
        role = Role.SELECTED if index == selected else STATE_ROLES.get(
            str(row.get("state")), Role.NORMAL,
        )
        lines.append(TuiLine(_columns([
            (row.get("state"), 15), (row.get("priority"), 5),
            (row.get("strategy"), 28), (row.get("id"), max(18, width - 75)),
            (row.get("attempts"), 6), (row.get("blocker_code"), 22),
        ], width), role))
    if not rows:
        lines.append(TuiLine("No unresolved queue items.", Role.MUTED))
    if detail_height and rows:
        row = rows[selected]
        lines.extend([
            TuiLine("─" * max(1, width), Role.MUTED),
            TuiLine(f"JOB     {_clip(row.get('id'), max(1, width - 8))}", Role.DETAIL),
            TuiLine(f"STRATEGY {_clip(row.get('strategy'), max(1, width - 10))}", Role.DETAIL),
            TuiLine(
                f"RETRY   {_clip(row.get('retry_after'), 24)}  "
                f"BLOCKER {_clip(row.get('blocker_code'), max(1, width - 43))}",
                Role.DETAIL,
            ),
            TuiLine(f"DETAIL  {_clip(row.get('blocker_detail'), max(1, width - 8))}", Role.DETAIL),
        ])
    return lines


def _candidates(model: dict[str, Any], state: TuiState, width: int, height: int) -> list[TuiLine]:
    rows = model["candidates"]
    visible, selected = _selected_window(rows, state, max(1, height - 2))
    lines = [
        TuiLine(f"CANDIDATES  {len(rows)}", Role.SECTION),
        TuiLine(_columns([
            ("VERDICT", 23), ("STRATEGY", 27), ("STAGE", 14),
            ("NET %", 9), ("DD %", 9), ("SHARPE", 9),
            ("EXPERIMENT", max(18, width - 97)),
        ], width), Role.TABLE_HEADER),
    ]
    for index, row in visible:
        lines.append(TuiLine(_columns([
            (row.get("verdict"), 23), (row.get("strategy"), 27),
            (row.get("lifecycle_stage"), 14),
            (row.get("net_profit_percentage"), 9),
            (row.get("max_drawdown_percentage"), 9),
            (row.get("sharpe_ratio"), 9),
            (row.get("experiment_id"), max(18, width - 97)),
        ], width), Role.SELECTED if index == selected else Role.CANDIDATE))
    if not rows:
        lines.append(TuiLine("No promotion or revision candidates.", Role.MUTED))
    return lines


def _hpo(model: dict[str, Any], state: TuiState, width: int, height: int) -> list[TuiLine]:
    rows = model["hpo"]
    visible, selected = _selected_window(rows, state, max(1, height - 2))
    lines = [
        TuiLine(f"HPO LIFECYCLE  {len(rows)} studies", Role.SECTION),
        TuiLine(_columns([
            ("STATE", 23), ("STRATEGY", 27), ("DONE", 7),
            ("TRIALS", 8), ("SELECT", 8), ("STUDY", 26),
            ("NEXT", max(18, width - 105)),
        ], width), Role.TABLE_HEADER),
    ]
    for index, row in visible:
        lines.append(TuiLine(_columns([
            (row.get("lifecycle_state"), 23), (row.get("strategy"), 27),
            (row.get("completed_trial_count"), 7), (row.get("trial_count"), 8),
            (row.get("selected_trial_count"), 8), (row.get("study_id"), 26),
            (row.get("next_action"), max(18, width - 105)),
        ], width), Role.SELECTED if index == selected else Role.HPO))
    if not rows:
        lines.append(TuiLine("No HPO studies.", Role.MUTED))
    return lines


def _memory(model: dict[str, Any], state: TuiState, width: int, height: int) -> list[TuiLine]:
    counts = model["memory"]
    rows = model["memories"]
    visible, selected = _selected_window(rows, state, max(1, height - 4))
    ready = not counts.get("pending") and not counts.get("retry")
    lines = [
        TuiLine("RESEARCH MEMORY", Role.SECTION),
        TuiLine(
            f"{'READY' if ready else 'ATTENTION'}  delivered={counts.get('delivered', 0)}  "
            f"pending={counts.get('pending', 0)}  retry={counts.get('retry', 0)}",
            Role.HEALTHY if ready else Role.WARNING,
        ),
        TuiLine(_columns([
            ("STATE", 12), ("STRATEGY", 28), ("STAGE", 16),
            ("VERDICT", 23), ("TRIES", 7), ("CREATED", max(20, width - 91)),
        ], width), Role.TABLE_HEADER),
    ]
    for index, row in visible:
        lines.append(TuiLine(_columns([
            (row.get("state"), 12), (row.get("strategy"), 28),
            (row.get("lifecycle_stage"), 16), (row.get("verdict"), 23),
            (row.get("attempts"), 7), (row.get("created_at"), max(20, width - 91)),
        ], width), Role.SELECTED if index == selected else MEMORY_STATE_ROLES.get(
            str(row.get("state")), Role.NORMAL,
        )))
    return lines


class TuiRenderer:
    """Responsive semantic renderer; curses color is applied only at the edge."""

    _VIEW_RENDERERS = {
        View.OVERVIEW: _overview,
        View.QUEUE: _queue,
        View.CANDIDATES: _candidates,
        View.HPO: _hpo,
        View.MEMORY: _memory,
    }

    def render(
        self, model: dict[str, Any], state: TuiState, *, width: int, height: int,
    ) -> list[TuiLine]:
        width = max(20, width)
        height = max(6, height)
        lines = _header(model, state, width)
        content_height = max(1, height - len(lines) - 2)
        lines.extend(self._VIEW_RENDERERS[state.view](
            model, state, width, content_height,
        ))
        if state.show_help:
            help_lines = [
                "KEYS", "1-5 / ←→ views    ↑↓ select    PgUp/PgDn scroll",
                "Enter/d details    p pause    r resume    s,s stop",
                "g top    G bottom    ? close help    q quit",
            ]
            lines = lines[:2] + [TuiLine(item, Role.HELP) for item in help_lines]
        footer = state.message or (
            "1-5 views  ↑↓ select  Enter detail  p pause  r resume  "
            "s stop  ? help  q quit"
        )
        lines = lines[:max(0, height - 1)]
        while len(lines) < height - 1:
            lines.append(TuiLine(""))
        lines.append(TuiLine(_clip(footer, width), Role.FOOTER))
        return [
            TuiLine(_clip(line.text, width), line.role) for line in lines[:height]
        ]

    @staticmethod
    def attributes() -> dict[Role, int]:
        attrs = {
            Role.NORMAL: curses.A_NORMAL,
            Role.SECTION: curses.A_BOLD,
            Role.TABLE_HEADER: curses.A_BOLD | curses.A_UNDERLINE,
            Role.TABS: curses.A_BOLD,
            Role.SELECTED: curses.A_REVERSE | curses.A_BOLD,
            Role.MUTED: curses.A_DIM,
            Role.DETAIL: curses.A_DIM,
            Role.FOOTER: curses.A_REVERSE,
            Role.HELP: curses.A_BOLD,
        }
        if not curses.has_colors():
            return attrs
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
        pairs = {
            Role.HEALTHY: curses.COLOR_GREEN,
            Role.WARNING: curses.COLOR_YELLOW,
            Role.ERROR: curses.COLOR_RED,
            Role.COMMAND: curses.COLOR_CYAN,
            Role.METRICS: curses.COLOR_CYAN,
            Role.RUNNING: curses.COLOR_GREEN,
            Role.READY: curses.COLOR_CYAN,
            Role.WAITING_RETRY: curses.COLOR_YELLOW,
            Role.BLOCKED: curses.COLOR_RED,
            Role.SCHEDULED: curses.COLOR_BLUE,
            Role.CANDIDATE: curses.COLOR_MAGENTA,
            Role.HPO: curses.COLOR_BLUE,
            Role.PENDING: curses.COLOR_YELLOW,
            Role.RETRY: curses.COLOR_RED,
            Role.DELIVERED: curses.COLOR_GREEN,
        }
        emphasized = {Role.HEALTHY, Role.WARNING, Role.ERROR}
        for index, (role, foreground) in enumerate(pairs.items(), start=1):
            try:
                curses.init_pair(index, foreground, -1)
                attrs[role] = curses.color_pair(index) | (
                    curses.A_BOLD if role in emphasized else 0
                )
            except curses.error:
                continue
        return attrs

    @staticmethod
    def draw(
        screen: Any, lines: list[TuiLine], attrs: dict[Role, int],
    ) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        for y, line in enumerate(lines[:height]):
            try:
                screen.addnstr(
                    y, 0, line.text, max(0, width - 1),
                    attrs.get(line.role, curses.A_NORMAL),
                )
            except curses.error:
                pass
        screen.refresh()


def render_tui(
    model: dict[str, Any], state: TuiState, *, width: int, height: int,
) -> list[TuiLine]:
    """Stable pure rendering seam for tests and non-curses previews."""
    return TuiRenderer().render(model, state, width=width, height=height)
