"""Relative, reproducible evaluation windows.

Route dates remain materialized in each experiment for reproducibility. The
default route plan is derived from an anchor date and durations in
``ResourcePolicy``; operators can pin the anchor in TOML for a repeatable
research cycle or provide explicit route files for a different thesis.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .resources import ResourcePolicy


@dataclass(frozen=True)
class EvaluationWindowPlan:
    anchor_date: date
    hpo_start: date
    hpo_finish: date
    rolling_start: date
    rolling_finish: date
    oos_start: date
    oos_finish: date

    def to_dict(self) -> dict[str, str]:
        return {
            name: value.isoformat()
            for name, value in (
                ("anchor_date", self.anchor_date),
                ("hpo_start", self.hpo_start),
                ("hpo_finish", self.hpo_finish),
                ("rolling_start", self.rolling_start),
                ("rolling_finish", self.rolling_finish),
                ("oos_start", self.oos_start),
                ("oos_finish", self.oos_finish),
            )
        }


def resolve_evaluation_windows(
    policy: ResourcePolicy | None = None,
    *,
    anchor_date: date | str | None = None,
) -> EvaluationWindowPlan:
    """Resolve relative windows as half-open [start, finish] date ranges.

    Adjacent windows share no candle days: each window's stored finish date
    is the day before the next window's start, because backtests include
    ``finish_date``. An explicit anchor is mandatory — pass ``anchor_date``
    or configure ``resources.evaluation_windows.as_of_date`` so cohorts stay
    comparable; wall-clock today is never used.
    """
    policy = policy or ResourcePolicy()
    configured = policy.evaluation_windows
    if configured.mode == "explicit":
        raise ValueError(
            "relative evaluation windows disabled; provide explicit route dates"
        )
    anchor = _coerce_date(anchor_date or configured.as_of_date)
    if anchor is None:
        raise ValueError(
            "evaluation windows require an explicit anchor date: set "
            "resources.evaluation_windows.as_of_date or pass anchor_date"
        )
    oos_start = anchor - timedelta(days=configured.oos_lookback_days)
    rolling_start = oos_start - timedelta(days=configured.rolling_lookback_days)
    hpo_start = rolling_start - timedelta(days=configured.comparison_lookback_days)
    return EvaluationWindowPlan(
        anchor_date=anchor,
        hpo_start=hpo_start,
        hpo_finish=rolling_start - timedelta(days=1),
        rolling_start=rolling_start,
        rolling_finish=oos_start - timedelta(days=1),
        oos_start=oos_start,
        oos_finish=anchor,
    )


def _coerce_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
