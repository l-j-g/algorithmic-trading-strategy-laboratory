"""Responsive semantic rendering for the ATS Lab terminal UI."""
from __future__ import annotations

import curses
from typing import Any, Protocol

from .terminal_table import FittedTable
from .tui_tables import (
    ACTIVE_COLUMNS,
    CANDIDATE_COLUMNS,
    HPO_COLUMNS,
    MEMORY_COLUMNS,
    ORG_COLUMNS,
    QUEUE_COLUMNS,
)
from .tui_types import (
    ColumnMode,
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


def _clip_line(value: object, width: int) -> str:
    """Clip a composed screen line without destroying column padding."""
    text = str(value)
    if width <= 0:
        return ""
    return text if len(text) <= width else text[:max(1, width - 1)] + "…"


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
    progress = str(snapshot.get("progress_state") or "healthy").upper()
    role = {
        "RUNNING": Role.RUNNING,
        "READY": Role.READY,
        "STALLED": Role.ERROR,
        "WAITING": Role.WARNING,
    }.get(progress, Role.HEALTHY if snapshot.get("healthy") else Role.ERROR)
    title = _columns([
        ("ATS LAB", 10),
        (progress, 12),
        (f"supervisor:{runtime.get('phase') or 'not_reported'}", 24),
        (f"control:{control.get('desired_state') or 'running'}", 22),
        (snapshot.get("checked_at"), max(0, width - 71)),
    ], width)
    tabs = "  ".join(
        f"[{view.value + 1} {view.label}]" if view == state.view
        else f" {view.value + 1} {view.label} "
        for view in View
    )
    controls = "[R START LOOP]  [P PAUSE]  [S,S STOP LOOP]  [Q CLOSE UI]"
    return [
        TuiLine(title, role),
        TuiLine(_clip_line(tabs, width), Role.TABS),
        TuiLine(_clip_line(controls, width), Role.COMMAND),
    ]


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
        TuiLine(FittedTable(ACTIVE_COLUMNS, width).render_header(), Role.TABLE_HEADER),
    ]
    active_table = FittedTable(ACTIVE_COLUMNS, width)
    for row in (snapshot.get("active_items") or [])[:max(1, height - len(lines) - 2)]:
        lines.append(TuiLine(
            active_table.render_row(row),
            STATE_ROLES.get(str(row.get("state")), Role.NORMAL),
        ))
    if not snapshot.get("active_items"):
        lines.append(TuiLine("No active work.", Role.MUTED))
    return lines


def _queue(model: dict[str, Any], state: TuiState, width: int, height: int) -> list[TuiLine]:
    rows = model["queue"]
    detail_height = 5 if state.show_detail and rows else 0
    available = max(1, height - 3 - detail_height)
    visible, selected = _selected_window(rows, state, available)
    table = FittedTable(QUEUE_COLUMNS, width)
    lines = [
        TuiLine(f"QUEUE  {len(rows)} unresolved jobs", Role.SECTION),
        TuiLine(table.render_header(), Role.TABLE_HEADER),
    ]
    for index, row in visible:
        role = Role.SELECTED if index == selected else STATE_ROLES.get(
            str(row.get("state")), Role.NORMAL,
        )
        lines.append(TuiLine(table.render_row(row), role))
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
    table = FittedTable(CANDIDATE_COLUMNS, width)
    lines = [
        TuiLine(f"CANDIDATES  {len(rows)}", Role.SECTION),
        TuiLine(table.render_header(), Role.TABLE_HEADER),
    ]
    for index, row in visible:
        lines.append(TuiLine(
            table.render_row(row),
            Role.SELECTED if index == selected else Role.CANDIDATE,
        ))
    if not rows:
        lines.append(TuiLine("No promotion or revision candidates.", Role.MUTED))
    return lines


def _hpo(model: dict[str, Any], state: TuiState, width: int, height: int) -> list[TuiLine]:
    rows = model["hpo"]
    visible, selected = _selected_window(rows, state, max(1, height - 2))
    table = FittedTable(HPO_COLUMNS, width)
    lines = [
        TuiLine(f"HPO LIFECYCLE  {len(rows)} studies", Role.SECTION),
        TuiLine(table.render_header(), Role.TABLE_HEADER),
    ]
    for index, row in visible:
        lines.append(TuiLine(
            table.render_row(row),
            Role.SELECTED if index == selected else Role.HPO,
        ))
    if not rows:
        lines.append(TuiLine("No HPO studies.", Role.MUTED))
    return lines


def _memory(model: dict[str, Any], state: TuiState, width: int, height: int) -> list[TuiLine]:
    counts = model["memory"]
    rows = model["memories"]
    visible, selected = _selected_window(rows, state, max(1, height - 4))
    ready = not counts.get("pending") and not counts.get("retry")
    table = FittedTable(MEMORY_COLUMNS, width)
    lines = [
        TuiLine("RESEARCH MEMORY", Role.SECTION),
        TuiLine(
            f"{'READY' if ready else 'ATTENTION'}  delivered={counts.get('delivered', 0)}  "
            f"pending={counts.get('pending', 0)}  retry={counts.get('retry', 0)}",
            Role.HEALTHY if ready else Role.WARNING,
        ),
        TuiLine(table.render_header(), Role.TABLE_HEADER),
    ]
    for index, row in visible:
        lines.append(TuiLine(table.render_row(row), Role.SELECTED if index == selected else MEMORY_STATE_ROLES.get(
            str(row.get("state")), Role.NORMAL,
        )))
    return lines


def _columns_view(
    model: dict[str, Any], state: TuiState, width: int, height: int,
) -> list[TuiLine]:
    rows = model["columns"]
    row_capacity = max(1, height - 2)
    while True:
        visible, selected = _selected_window(rows, state, row_capacity)
        visible_groups = sum(
            1 for index, (_, row) in enumerate(visible)
            if index == 0
            or row.get("state") != visible[index - 1][1].get("state")
        )
        fitted_capacity = max(1, height - 2 - visible_groups)
        if fitted_capacity >= row_capacity:
            break
        row_capacity = fitted_capacity
    columns = tuple(
        item.column for item in ORG_COLUMNS
        if item.minimum_mode <= state.column_mode
    )
    table = FittedTable(columns, width)
    lines = [
        TuiLine(
            f"ORG COLUMNS  profile={state.column_mode.label}  "
            "press c to cycle",
            Role.SECTION,
        ),
        TuiLine(table.render_header(), Role.TABLE_HEADER),
    ]
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("state") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    previous_state: str | None = None
    for index, row in visible:
        row_state = str(row.get("state") or "unknown")
        if row_state != previous_state:
            lines.append(TuiLine(
                f"* {row_state.replace('_', ' ').upper()} ({counts[row_state]})",
                Role.GROUP,
            ))
            previous_state = row_state
        item = dict(row)
        item["item"] = "  " + str(item.get("item") or "")
        lines.append(TuiLine(
            table.render_row(item),
            Role.SELECTED if index == selected else STATE_ROLES.get(
                row_state, Role.NORMAL,
            ),
        ))
    if not rows:
        lines.append(TuiLine("No unresolved work for column view.", Role.MUTED))
    return lines


class TuiRenderer:
    """Responsive semantic renderer; curses color is applied only at the edge."""

    _VIEW_RENDERERS = {
        View.OVERVIEW: _overview,
        View.QUEUE: _queue,
        View.CANDIDATES: _candidates,
        View.HPO: _hpo,
        View.MEMORY: _memory,
        View.COLUMNS: _columns_view,
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
                "KEYS", "1-6 / ←→ views    ↑↓ select    PgUp/PgDn scroll",
                "Enter/d details    r start loop    p pause    s,s stop loop",
                "c column profile    g top    G bottom    ? help    q quit",
            ]
            lines = lines[:2] + [TuiLine(item, Role.HELP) for item in help_lines]
        footer = state.message or (
            "R start loop  P pause  S,S stop loop  1-6 views  c columns  "
            "? help  Q close UI"
        )
        lines = lines[:max(0, height - 1)]
        while len(lines) < height - 1:
            lines.append(TuiLine(""))
        lines.append(TuiLine(_clip_line(footer, width), Role.FOOTER))
        return [
            TuiLine(_clip_line(line.text, width), line.role)
            for line in lines[:height]
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
            Role.GROUP: curses.A_BOLD,
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
