from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import ExperimentSpec, WorkItem, WorkState
from ats_lab.terminal_ui import (
    Action,
    ColumnMode,
    Role,
    TuiController,
    TuiLine,
    TuiState,
    View,
    build_tui_model,
    handle_key,
    render_tui,
)
from ats_lab.loop_control import LoopStatus


class TerminalUiTests(unittest.TestCase):
    def make_database(self, root: str) -> WorkflowDatabase:
        database = WorkflowDatabase(Path(root) / "workflow.sqlite3")
        database.initialize()
        database.upsert_experiment(ExperimentSpec(
            id="EXP-1", strategy_name="ResponsiveTrendStrategy",
        ))
        database.upsert_work_item(WorkItem(
            id="JOB-1", experiment_id="EXP-1", priority=3,
            state=WorkState.READY,
        ))
        return database

    def test_repository_and_renderer_produce_bounded_responsive_views(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            model = build_tui_model(self.make_database(tmp))
            overview = render_tui(
                model, TuiState(), width=80, height=20,
            )
            queue_state = TuiState(view=View.QUEUE)
            queue = render_tui(
                model, queue_state, width=120, height=20,
            )
            narrow = render_tui(
                model, TuiState(view=View.QUEUE), width=42, height=10,
            )
            columns = render_tui(
                model, TuiState(
                    view=View.COLUMNS, column_mode=ColumnMode.WIDE,
                ), width=180, height=20,
            )

        self.assertEqual(len(overview), 20)
        self.assertTrue(all(len(line.text) <= 80 for line in overview))
        self.assertTrue(any("ATS LAB" in line.text for line in overview))
        self.assertTrue(any("JOB-1" in line.text for line in queue))
        self.assertTrue(any(line.role is Role.SELECTED for line in queue))
        self.assertTrue(all(len(line.text) <= 42 for line in narrow))
        column_text = "\n".join(line.text for line in columns)
        self.assertIn("* READY (1)", column_text)
        self.assertIn("NET %", column_text)
        self.assertIn("SHARPE", column_text)
        self.assertIn("JOB-1", column_text)

    def test_navigation_and_controls_use_typed_actions(self) -> None:
        state = TuiState()

        self.assertIsNone(handle_key(state, ord("2"), 3))
        self.assertIs(state.view, View.QUEUE)
        handle_key(state, ord("6"), 3)
        self.assertIs(state.view, View.COLUMNS)
        original_mode = state.column_mode
        handle_key(state, ord("c"), 3)
        self.assertIsNot(state.column_mode, original_mode)
        handle_key(state, curses.KEY_DOWN, 3)
        self.assertEqual(state.selected, 1)
        handle_key(state, ord("?"), 3)
        self.assertTrue(state.show_help)
        handle_key(state, ord("?"), 3)
        self.assertFalse(state.show_help)
        self.assertIsNone(handle_key(state, ord("s"), 3))
        self.assertTrue(state.confirm_stop)
        self.assertIs(handle_key(state, ord("s"), 3), Action.STOP)
        self.assertIs(handle_key(state, ord("p"), 3), Action.PAUSE)
        self.assertIs(handle_key(state, ord("r"), 3), Action.START)
        self.assertIs(handle_key(state, ord("q"), 3), Action.QUIT)

    def test_controller_depends_on_replaceable_repository_and_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)

            class FakeRepository:
                def load(self):
                    return {
                        "snapshot": {}, "queue": [], "candidates": [],
                        "hpo": [], "memories": [], "memory": {},
                        "columns": [],
                        "guidance": {},
                    }

            class FakeRenderer:
                def attributes(self):
                    return {}

                def render(self, model, state, *, width, height):
                    return [TuiLine("fake")]

                def draw(self, screen, lines, attrs):
                    screen.drawn = lines

            class FakeScreen:
                drawn = []
                keys = [ord("r"), ord("p"), ord("s"), ord("s"), ord("q")]

                def keypad(self, value):
                    return None

                def timeout(self, value):
                    return None

                def getmaxyx(self):
                    return (10, 40)

                def getch(self):
                    return self.keys.pop(0)

            class FakeLoopControl:
                calls = []

                def start(self):
                    self.calls.append("start")
                    return LoopStatus("started", 123, "starting", "running")

                def pause(self):
                    self.calls.append("pause")
                    return LoopStatus("paused", 123, "idle", "paused")

                def stop(self):
                    self.calls.append("stop")
                    return LoopStatus(
                        "stop_requested", 123, "idle", "stop_requested",
                    )

                def status(self):
                    return LoopStatus("running", 123, "idle", "running")

            lifecycle = FakeLoopControl()
            controller = TuiController(
                database, repository=FakeRepository(), renderer=FakeRenderer(),
                loop_control=lifecycle,
            )
            screen = FakeScreen()

            result = controller.run(screen)


        self.assertEqual(result, 0)
        self.assertEqual(screen.drawn, [TuiLine("fake")])
        self.assertEqual(lifecycle.calls, ["start", "pause", "stop"])


if __name__ == "__main__":
    unittest.main()
