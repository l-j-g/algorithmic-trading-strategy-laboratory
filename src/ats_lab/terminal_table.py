"""Reusable terminal-width table layout with stable titled columns."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Iterable, Mapping


class Alignment(StrEnum):
    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True)
class TableColumn:
    key: str
    title: str
    preferred_width: int
    minimum_width: int
    priority: int = 50
    alignment: Alignment = Alignment.LEFT
    required: bool = False
    expand: bool = False

    def __post_init__(self) -> None:
        if self.minimum_width < 1:
            raise ValueError("minimum column width must be positive")
        if self.preferred_width < self.minimum_width:
            raise ValueError("preferred width cannot be below minimum width")


@dataclass(frozen=True)
class FittedTable:
    """Fit, title, and render one table without breaking column alignment."""

    columns: tuple[TableColumn, ...]
    width: int
    gap: int = 2

    def fitted_columns(self) -> tuple[TableColumn, ...]:
        available = max(1, self.width)
        active = list(self.columns)

        def required_width(use_preferred: bool) -> int:
            widths = (
                column.preferred_width if use_preferred
                else max(column.minimum_width, len(column.title))
                for column in active
            )
            return sum(widths) + self.gap * max(0, len(active) - 1)

        while required_width(False) > available:
            optional = [column for column in active if not column.required]
            if not optional:
                break
            active.remove(max(optional, key=lambda column: column.priority))

        widths = {
            column.key: max(column.minimum_width, len(column.title))
            for column in active
        }
        remaining = max(
            0,
            available - sum(widths.values())
            - self.gap * max(0, len(active) - 1),
        )
        for column in sorted(active, key=lambda item: item.priority):
            growth = min(
                remaining,
                max(0, column.preferred_width - widths[column.key]),
            )
            widths[column.key] += growth
            remaining -= growth
        expandable = [column for column in active if column.expand]
        for index, column in enumerate(expandable):
            share = remaining // (len(expandable) - index)
            widths[column.key] += share
            remaining -= share
        return tuple(
            replace(column, preferred_width=widths[column.key])
            for column in active
        )

    @staticmethod
    def _cell(value: object, width: int, alignment: Alignment) -> str:
        text = "—" if value in (None, "") else " ".join(str(value).split())
        if len(text) > width:
            text = text[:max(1, width - 1)] + "…"
        return text.rjust(width) if alignment is Alignment.RIGHT else text.ljust(width)

    def render_header(self) -> str:
        return self._render({column.key: column.title for column in self.columns})

    def render_row(self, row: Mapping[str, Any]) -> str:
        return self._render(row)

    def render_rows(self, rows: Iterable[Mapping[str, Any]]) -> list[str]:
        return [self.render_row(row) for row in rows]

    def _render(self, row: Mapping[str, Any]) -> str:
        fitted = self.fitted_columns()
        return (" " * self.gap).join(
            self._cell(row.get(column.key), column.preferred_width, column.alignment)
            for column in fitted
        )
