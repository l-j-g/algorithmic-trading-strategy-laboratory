"""Small deterministic multi-strategy preflight.

This is an evaluation aid, not a promotion path. It consumes aligned return
observations supplied by an operator or a future portfolio evidence importer;
it never infers correlation from aggregate profit or changes ATS state.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Iterable


@dataclass(frozen=True)
class PortfolioCandidate:
    strategy: str
    returns: tuple[float, ...] = ()
    capacity_notional: float | None = None
    proposed_allocation_notional: float | None = None


@dataclass(frozen=True)
class PairwiseCorrelation:
    left: str
    right: str
    value: float | None


@dataclass(frozen=True)
class PortfolioAssessment:
    candidate_count: int
    correlations: tuple[PairwiseCorrelation, ...]
    high_correlation_pairs: tuple[PairwiseCorrelation, ...]
    capacity_utilization: tuple[tuple[str, float], ...]
    blocking_reasons: tuple[str, ...]

    @property
    def ready_for_portfolio_review(self) -> bool:
        return not self.blocking_reasons

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_count": self.candidate_count,
            "correlations": [
                {"left": item.left, "right": item.right, "value": item.value}
                for item in self.correlations
            ],
            "high_correlation_pairs": [
                {"left": item.left, "right": item.right, "value": item.value}
                for item in self.high_correlation_pairs
            ],
            "capacity_utilization": [
                {"strategy": strategy, "utilization": utilization}
                for strategy, utilization in self.capacity_utilization
            ],
            "blocking_reasons": list(self.blocking_reasons),
            "ready_for_portfolio_review": self.ready_for_portfolio_review,
        }


def assess_portfolio(
    candidates: Iterable[PortfolioCandidate],
    *,
    correlation_threshold: float = 0.85,
    capacity_utilization_limit: float = 0.70,
) -> PortfolioAssessment:
    """Flag concentration and capacity risks without making trade decisions."""
    if not 0 < correlation_threshold <= 1:
        raise ValueError("correlation_threshold must be in (0, 1]")
    if not 0 < capacity_utilization_limit <= 1:
        raise ValueError("capacity_utilization_limit must be in (0, 1]")
    items = tuple(candidates)
    names = [item.strategy for item in items]
    if len(names) != len(set(names)):
        raise ValueError("portfolio candidate strategy names must be unique")

    correlations: list[PairwiseCorrelation] = []
    high: list[PairwiseCorrelation] = []
    missing_correlation = False
    for index, left in enumerate(items):
        for right in items[index + 1:]:
            value = _pearson(left.returns, right.returns)
            pair = PairwiseCorrelation(left.strategy, right.strategy, value)
            correlations.append(pair)
            if value is None:
                missing_correlation = True
            elif abs(value) >= correlation_threshold:
                high.append(pair)

    utilization: list[tuple[str, float]] = []
    capacity_missing = False
    over_capacity: list[str] = []
    for item in items:
        if item.proposed_allocation_notional is None:
            continue
        if item.capacity_notional is None or item.capacity_notional <= 0:
            capacity_missing = True
            continue
        ratio = item.proposed_allocation_notional / item.capacity_notional
        utilization.append((item.strategy, ratio))
        if ratio > capacity_utilization_limit:
            over_capacity.append(item.strategy)

    reasons: list[str] = []
    if len(items) > 1 and missing_correlation:
        reasons.append("correlation_data_incomplete")
    if high:
        reasons.append("high_correlation")
    if capacity_missing:
        reasons.append("capacity_data_incomplete")
    if over_capacity:
        reasons.append("capacity_utilization_exceeded")
    return PortfolioAssessment(
        candidate_count=len(items),
        correlations=tuple(correlations),
        high_correlation_pairs=tuple(high),
        capacity_utilization=tuple(utilization),
        blocking_reasons=tuple(dict.fromkeys(reasons)),
    )


def _pearson(left: tuple[float, ...], right: tuple[float, ...]) -> float | None:
    if len(left) < 2 or len(left) != len(right):
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_delta = [value - left_mean for value in left]
    right_delta = [value - right_mean for value in right]
    left_norm = sqrt(sum(value * value for value in left_delta))
    right_norm = sqrt(sum(value * value for value in right_delta))
    if left_norm == 0 or right_norm == 0:
        return None
    return sum(a * b for a, b in zip(left_delta, right_delta)) / (left_norm * right_norm)
