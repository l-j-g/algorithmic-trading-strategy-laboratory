"""Named table definitions for the ATS Lab terminal operator interface."""
from __future__ import annotations

from dataclasses import dataclass

from .terminal_table import Alignment, TableColumn
from .tui_types import ColumnMode


def _column(
    key: str,
    title: str,
    preferred: int,
    minimum: int,
    priority: int,
    *,
    numeric: bool = False,
    required: bool = False,
    expand: bool = False,
) -> TableColumn:
    return TableColumn(
        key, title, preferred, minimum, priority,
        alignment=Alignment.RIGHT if numeric else Alignment.LEFT,
        required=required,
        expand=expand,
    )


QUEUE_COLUMNS = (
    _column("state", "STATE", 15, 11, 1, required=True),
    _column("priority", "PRI", 4, 3, 8, numeric=True),
    _column("strategy", "STRATEGY", 28, 14, 2, required=True),
    _column("id", "JOB", 34, 14, 3, required=True, expand=True),
    _column("attempts", "TRIES", 5, 5, 7, numeric=True),
    _column("blocker_code", "BLOCKER", 24, 10, 6),
)

ACTIVE_COLUMNS = tuple(
    column for column in QUEUE_COLUMNS
    if column.key in {"state", "priority", "strategy", "id", "blocker_code"}
)

CANDIDATE_COLUMNS = (
    _column("verdict", "VERDICT", 23, 10, 1, required=True),
    _column("strategy", "STRATEGY", 27, 14, 2, required=True),
    _column("lifecycle_stage", "STAGE", 14, 8, 7),
    _column("net_profit_percentage", "NET %", 9, 6, 3, numeric=True),
    _column("max_drawdown_percentage", "DD %", 9, 5, 4, numeric=True),
    _column("sharpe_ratio", "SHARPE", 9, 6, 5, numeric=True),
    _column("experiment_id", "EXPERIMENT", 30, 14, 6, expand=True),
)

HPO_COLUMNS = (
    _column("lifecycle_state", "STATE", 21, 10, 1, required=True),
    _column("strategy", "STRATEGY", 27, 14, 2, required=True),
    _column("completed_trial_count", "DONE", 5, 4, 5, numeric=True),
    _column("trial_count", "TRIALS", 6, 6, 6, numeric=True),
    _column("selected_trial_count", "SELECT", 6, 6, 7, numeric=True),
    _column("study_id", "STUDY", 27, 12, 3),
    _column("next_action", "NEXT", 30, 12, 4, expand=True),
)

MEMORY_COLUMNS = (
    _column("state", "STATE", 12, 8, 1, required=True),
    _column("strategy", "STRATEGY", 28, 14, 2, required=True),
    _column("lifecycle_stage", "STAGE", 16, 8, 7),
    _column("verdict", "VERDICT", 23, 10, 3),
    _column("attempts", "TRIES", 5, 5, 6, numeric=True),
    _column("created_at", "CREATED", 27, 12, 5),
)


@dataclass(frozen=True)
class ProfiledColumn:
    column: TableColumn
    minimum_mode: ColumnMode = ColumnMode.COMPACT


ORG_COLUMNS = (
    ProfiledColumn(_column("item", "ITEM", 31, 16, 1, required=True)),
    ProfiledColumn(_column("state", "STATE", 15, 11, 1, required=True)),
    ProfiledColumn(_column("priority", "PRI", 4, 3, 8, numeric=True)),
    ProfiledColumn(
        _column("experiment_type", "TYPE", 14, 7, 10), ColumnMode.STANDARD,
    ),
    ProfiledColumn(_column("strategy", "STRATEGY", 25, 14, 2, required=True)),
    ProfiledColumn(_column("symbol", "SYMBOL", 11, 7, 9), ColumnMode.WIDE),
    ProfiledColumn(_column("timeframe", "TF", 5, 2, 9), ColumnMode.WIDE),
    ProfiledColumn(_column("verdict", "VERDICT", 22, 10, 7), ColumnMode.STANDARD),
    ProfiledColumn(
        _column("net_profit_percentage", "NET %", 9, 6, 3, numeric=True),
        ColumnMode.WIDE,
    ),
    ProfiledColumn(
        _column("sharpe_ratio", "SHARPE", 8, 6, 4, numeric=True),
        ColumnMode.WIDE,
    ),
    ProfiledColumn(
        _column("trade_count", "TRADES", 7, 6, 6, numeric=True),
        ColumnMode.WIDE,
    ),
    ProfiledColumn(_column(
        "next", "NEXT / BLOCKER", 38, 14, 2, required=True, expand=True,
    )),
)
