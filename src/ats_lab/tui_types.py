"""Typed value objects shared by ATS Lab terminal UI components."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

from .models import Verdict, WorkState


class View(IntEnum):
    OVERVIEW = 0
    QUEUE = 1
    CANDIDATES = 2
    HPO = 3
    MEMORY = 4
    COLUMNS = 5

    @property
    def label(self) -> str:
        if self is View.HPO:
            return "HPO"
        return self.name.replace("_", " ").title()


class Role(StrEnum):
    NORMAL = "normal"
    HEALTHY = "healthy"
    WARNING = "warning"
    ERROR = "error"
    COMMAND = "command"
    METRICS = "metrics"
    SECTION = "section"
    TABLE_HEADER = "table_header"
    TABS = "tabs"
    SELECTED = "selected"
    MUTED = "muted"
    DETAIL = "detail"
    FOOTER = "footer"
    HELP = "help"
    RUNNING = "running"
    READY = "ready"
    WAITING_RETRY = "waiting_retry"
    BLOCKED = "blocked"
    SCHEDULED = "scheduled"
    CANDIDATE = "candidate"
    HPO = "hpo"
    PENDING = "pending"
    RETRY = "retry"
    DELIVERED = "delivered"
    GROUP = "group"


class ColumnMode(IntEnum):
    COMPACT = 0
    STANDARD = 1
    WIDE = 2

    @property
    def label(self) -> str:
        return self.name.lower()

    def next(self) -> ColumnMode:
        return ColumnMode((self + 1) % len(ColumnMode))


class Action(StrEnum):
    QUIT = "quit"
    START = "start"
    PAUSE = "pause"
    STOP = "stop"


class ControlState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOP_REQUESTED = "stop_requested"


QUEUE_STATE_ORDER = (
    WorkState.RUNNING,
    WorkState.READY,
    WorkState.WAITING_RETRY,
    WorkState.SCHEDULED,
    WorkState.BLOCKED,
)

CANDIDATE_VERDICT_ORDER = (
    Verdict.PAPER_TRADE_CANDIDATE,
    Verdict.HPO_CANDIDATE,
    Verdict.REVISE,
)

STATE_ROLES = {
    WorkState.RUNNING.value: Role.RUNNING,
    WorkState.READY.value: Role.READY,
    WorkState.WAITING_RETRY.value: Role.WAITING_RETRY,
    WorkState.BLOCKED.value: Role.BLOCKED,
    WorkState.SCHEDULED.value: Role.SCHEDULED,
}

MEMORY_STATE_ROLES = {
    "pending": Role.PENDING,
    "retry": Role.RETRY,
    "delivered": Role.DELIVERED,
}


@dataclass
class TuiState:
    view: View = View.OVERVIEW
    selected: int = 0
    scroll: int = 0
    show_help: bool = False
    show_detail: bool = True
    confirm_stop: bool = False
    message: str = ""
    column_mode: ColumnMode = ColumnMode.STANDARD


@dataclass(frozen=True)
class TuiLine:
    text: str
    role: Role = Role.NORMAL
