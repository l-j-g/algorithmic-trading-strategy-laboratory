"""Public façade for the dependency-free ATS Lab terminal UI."""
from .tui_controller import TuiController, handle_key, row_count, run_tui
from .tui_renderer import ScreenRenderer, TuiRenderer, render_tui
from .tui_repository import TuiDataSource, TuiRepository, build_tui_model
from .tui_types import (
    Action,
    ColumnMode,
    ControlState,
    Role,
    TuiLine,
    TuiState,
    View,
)
from .terminal_table import Alignment, FittedTable, TableColumn

__all__ = [
    "Action",
    "Alignment",
    "ColumnMode",
    "ControlState",
    "FittedTable",
    "Role",
    "ScreenRenderer",
    "TuiController",
    "TuiDataSource",
    "TuiLine",
    "TuiRenderer",
    "TuiRepository",
    "TuiState",
    "TableColumn",
    "View",
    "build_tui_model",
    "handle_key",
    "render_tui",
    "row_count",
    "run_tui",
]
