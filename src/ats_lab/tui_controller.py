"""Input, refresh, and control orchestration for ATS Lab terminal UI."""
from __future__ import annotations

import curses
import time
from typing import Any, Callable

from .database import WorkflowDatabase
from .tui_renderer import ScreenRenderer, TuiRenderer
from .tui_repository import TuiDataSource, TuiRepository
from .tui_types import Action, CONTROL_TARGETS, TuiState, View


def row_count(model: dict[str, Any], view: View) -> int:
    rows = {
        View.OVERVIEW: (),
        View.QUEUE: model["queue"],
        View.CANDIDATES: model["candidates"],
        View.HPO: model["hpo"],
        View.MEMORY: model["memories"],
        View.COLUMNS: model["columns"],
    }
    return len(rows[view])


def handle_key(state: TuiState, key: int, available_rows: int) -> Action | None:
    """Pure navigation state transition; external control is returned as action."""
    if state.show_help:
        if key in (ord("?"), 27, ord("q")):
            state.show_help = False
        return None
    if key == ord("?"):
        state.show_help = True
        return None
    if key in (ord("q"), ord("Q")):
        return Action.QUIT
    if ord("1") <= key <= ord("6"):
        state.view = View(key - ord("1"))
        state.selected = state.scroll = 0
    elif key == curses.KEY_LEFT:
        state.view = View((state.view - 1) % len(View))
        state.selected = state.scroll = 0
    elif key == curses.KEY_RIGHT:
        state.view = View((state.view + 1) % len(View))
        state.selected = state.scroll = 0
    elif key in (curses.KEY_UP, ord("k")):
        state.selected = max(0, state.selected - 1)
    elif key in (curses.KEY_DOWN, ord("j")):
        state.selected = min(max(0, available_rows - 1), state.selected + 1)
    elif key == curses.KEY_PPAGE:
        state.selected = max(0, state.selected - 10)
    elif key == curses.KEY_NPAGE:
        state.selected = min(max(0, available_rows - 1), state.selected + 10)
    elif key == ord("g"):
        state.selected = 0
    elif key == ord("G"):
        state.selected = max(0, available_rows - 1)
    elif key in (10, 13, ord("d")):
        state.show_detail = not state.show_detail
    elif key == ord("c"):
        state.column_mode = state.column_mode.next()
        state.message = f"Column profile: {state.column_mode.label}"
    elif key == ord("p"):
        state.confirm_stop = False
        return Action.PAUSE
    elif key == ord("r"):
        state.confirm_stop = False
        return Action.RESUME
    elif key == ord("s"):
        if state.confirm_stop:
            state.confirm_stop = False
            return Action.STOP
        state.confirm_stop = True
        state.message = "Press s again to confirm graceful supervisor stop"
    else:
        state.confirm_stop = False
    return None


class TuiController:
    """Coordinates abstractions; data loading and rendering remain replaceable."""

    def __init__(
        self,
        database: WorkflowDatabase,
        *,
        interval: float = 1.0,
        repository: TuiDataSource | None = None,
        renderer: ScreenRenderer | None = None,
    ) -> None:
        if interval <= 0:
            raise ValueError("TUI interval must be positive")
        self.database = database
        self.interval = interval
        self.repository = repository or TuiRepository(database)
        self.renderer = renderer or TuiRenderer()
        self.state = TuiState()

    def run(self, screen: Any) -> int:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        attrs = self.renderer.attributes()
        screen.keypad(True)
        screen.timeout(200)
        model: dict[str, Any] | None = None
        refreshed_at = 0.0
        while True:
            now = time.monotonic()
            if model is None or now - refreshed_at >= self.interval:
                try:
                    model = self.repository.load()
                    if not self.state.confirm_stop:
                        self.state.message = ""
                except Exception as error:
                    self.state.message = f"Refresh failed: {type(error).__name__}"
                refreshed_at = now
            height, width = screen.getmaxyx()
            if model is not None:
                self.renderer.draw(screen, self.renderer.render(
                    model, self.state, width=width, height=height,
                ), attrs)
            key = screen.getch()
            if key == -1 or model is None:
                continue
            action = handle_key(
                self.state, key, row_count(model, self.state.view),
            )
            if action is Action.QUIT:
                return 0
            if action in CONTROL_TARGETS:
                desired = CONTROL_TARGETS[action]
                self.database.set_control_state(
                    desired, updated_by=f"tui:{action.value}",
                )
                self.state.message = f"Control set to {desired}"
                model = None
            elif key not in (-1, ord("s")):
                self.state.confirm_stop = False


def run_tui(
    database: WorkflowDatabase,
    *,
    interval: float = 1.0,
    wrapper: Callable[[Callable[[Any], int]], int] = curses.wrapper,
) -> int:
    return wrapper(TuiController(database, interval=interval).run)
