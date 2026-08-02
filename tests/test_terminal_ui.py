from __future__ import annotations

import curses
import tempfile
import unittest
from pathlib import Path

from ats_lab.database import WorkflowDatabase
from ats_lab.models import ExperimentSpec, WorkItem, WorkState
from ats_lab.terminal_ui import (
    Action,
    Role,
    TuiController,
    TuiLine,
    TuiState,
    View,
    build_tui_model,
    handle_key,
    render_tui,
)


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

        self.assertEqual(len(overview), 20)
        self.assertTrue(all(len(line.text) <= 80 for line in overview))
        self.assertTrue(any("ATS LAB" in line.text for line in overview))
        self.assertTrue(any("JOB-1" in line.text for line in queue))
        self.assertTrue(any(line.role is Role.SELECTED for line in queue))
        self.assertTrue(all(len(line.text) <= 42 for line in narrow))

    def test_navigation_and_controls_use_typed_actions(self) -> None:
        state = TuiState()

        self.assertIsNone(handle_key(state, ord("2"), 3))
        self.assertIs(state.view, View.QUEUE)
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
        self.assertIs(handle_key(state, ord("r"), 3), Action.RESUME)
        self.assertIs(handle_key(state, ord("q"), 3), Action.QUIT)

    def test_controller_depends_on_replaceable_repository_and_renderer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = self.make_database(tmp)

            class FakeRepository:
                def load(self):
                    return {
                        "snapshot": {}, "queue": [], "candidates": [],
                        "hpo": [], "memories": [], "memory": {},
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
                keys = [ord("p"), ord("q")]

                def keypad(self, value):
                    return None

                def timeout(self, value):
                    return None

                def getmaxyx(self):
                    return (10, 40)

                def getch(self):
                    return self.keys.pop(0)

            controller = TuiController(
                database, repository=FakeRepository(), renderer=FakeRenderer(),
            )
            screen = FakeScreen()

            result = controller.run(screen)

            control = database.control_status()["desired_state"]

        self.assertEqual(result, 0)
        self.assertEqual(screen.drawn, [TuiLine("fake")])
        self.assertEqual(control, "paused")


if __name__ == "__main__":
    unittest.main()
