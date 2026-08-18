"""Deterministic research gates over the canonical evidence contract."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .evidence import NormalizedEvidence
from .models import Verdict
from .resources import ResourcePolicy


@dataclass(frozen=True)
class GateDecision:
    verdict: Verdict
    passed: tuple[str, ...]
    failed: tuple[str, ...]
    missing: tuple[str, ...]
    holdout_degradation_percentage: float | None = None

    @property
    def finding(self) -> str:
        parts = []
        if self.failed:
            parts.append("failed=" + ",".join(self.failed))
        if self.missing:
            parts.append("missing=" + ",".join(self.missing))
        if not parts:
            parts.append("all deterministic gates passed")
        return "Deterministic gates: " + "; ".join(parts) + "."


@dataclass(frozen=True)
class PromotionDecision:
    """Evidence gate for claims that a strategy is ready for paper review.

    Baseline and optimization evidence can remain useful for analysis without
    being treated as deployable research.  This separate decision keeps those
    stages permissive while requiring validation evidence for promotion.
    """

    allowed: bool
    failed: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def finding(self) -> str:
        parts = []
        if self.failed:
            parts.append("failed=" + ",".join(self.failed))
        if self.missing:
            parts.append("missing=" + ",".join(self.missing))
        if not parts:
            parts.append("validation and cost-stress evidence passed")
        return "Promotion evidence: " + "; ".join(parts) + "."


def evaluate_promotion(
    evidence: Iterable[NormalizedEvidence],
    *,
    policy: ResourcePolicy,
) -> PromotionDecision:
    """Require validation, candle Monte Carlo, walk-forward, and cost stress.

    This is intentionally separate from :func:`evaluate_gates`: a profitable
    baseline remains valid research evidence and should still reach analysis,
    while a paper-trade claim must survive an unseen window and fee stress.
    """
    rows = tuple(evidence)
    validation = tuple(
        row for row in rows
        if row.evidence_split in {"oos", "rolling"}
        or row.lifecycle_stage == "out_of_sample"
    )
    failed: list[str] = []
    missing: list[str] = []
    if not validation:
        missing.append("oos_or_rolling")
    else:
        quality = evaluate_gates(validation, policy=policy)
        failed.extend(quality.failed)
        missing.extend(
            name for name in quality.missing
            if name != "fees_cost_sensitivity"
        )

    monte_carlo = tuple(
        row for row in rows if row.lifecycle_stage == "monte_carlo"
    )
    if not monte_carlo:
        missing.append("candle_based_monte_carlo")
    else:
        if any(row.monte_carlo_method != "candle_based" for row in monte_carlo):
            failed.append("candle_based_monte_carlo")
        scenarios = [row.monte_carlo_scenarios for row in monte_carlo]
        if any(value is None for value in scenarios):
            missing.append("monte_carlo_scenarios")
        elif any(value < policy.monte_carlo_scenarios for value in scenarios):
            failed.append("monte_carlo_scenarios")

    walk_forward = tuple(
        row for row in rows
        if row.evidence_split == "rolling"
        and row.walk_forward_method in {"walk_forward", "rolling"}
    )
    if not walk_forward:
        missing.append("walk_forward")
    else:
        windows = [row.walk_forward_windows for row in walk_forward]
        if any(value is None for value in windows):
            missing.append("walk_forward_windows")
        elif any(value < 2 for value in windows):
            failed.append("walk_forward_windows")

    cost_rows = tuple(
        row for row in rows
        if row.lifecycle_stage == "cost_sensitivity"
        or row.cost_stress_status is not None
    )
    if not cost_rows:
        missing.append("fees_cost_sensitivity")
    elif any(row.cost_stress_status == "fail" for row in cost_rows):
        failed.append("fees_cost_sensitivity")
    elif any(row.cost_stress_status is None for row in cost_rows):
        missing.append("fees_cost_sensitivity")

    # Stable order makes findings and tests deterministic when rows arrive in
    # different route order.
    failed = list(dict.fromkeys(failed))
    missing = list(dict.fromkeys(missing))
    return PromotionDecision(
        allowed=not failed and not missing,
        failed=tuple(failed),
        missing=tuple(missing),
    )


def evaluate_gates(
    evidence: Iterable[NormalizedEvidence],
    *,
    policy: ResourcePolicy,
    expected_routes: Iterable[Mapping[str, object]] = (),
    observed_routes: Iterable[Mapping[str, object]] = (),
) -> GateDecision:
    """Evaluate completion and quality without consulting raw run payloads."""
    rows = tuple(evidence)
    passed: list[str] = []
    failed: list[str] = []
    missing: list[str] = []

    expected = {
        (
            route.get("symbol"), route.get("timeframe"),
            route.get("start_date"), route.get("finish_date"),
        )
        for route in expected_routes
    }
    observed = {
        (row.symbol, row.timeframe, row.start_date, row.finish_date)
        for row in rows
    } | {
        (
            route.get("symbol"), route.get("timeframe"),
            route.get("start_date"), route.get("finish_date"),
        )
        for route in observed_routes
    }
    if expected:
        (passed if expected <= observed else failed).append("route_completion")
    elif rows:
        passed.append("route_completion")
    else:
        failed.append("route_completion")

    _minimum_gate(
        "minimum_trades", rows, "trade_count", policy.minimum_trades,
        passed, failed, missing,
    )
    _minimum_gate(
        "net_profit", rows, "net_profit_percentage", 0.0,
        passed, failed, missing, strict=True,
    )
    _maximum_gate(
        "max_drawdown", rows, "max_drawdown_percentage",
        policy.maximum_drawdown_percentage, passed, failed, missing,
    )
    _minimum_gate(
        "sharpe", rows, "sharpe_ratio", policy.minimum_sharpe_ratio,
        passed, failed, missing,
    )
    _minimum_gate(
        "profit_factor", rows, "profit_factor",
        policy.minimum_profit_factor, passed, failed, missing,
    )

    cost_rows = [
        row for row in rows
        if row.lifecycle_stage == "cost_sensitivity"
        or row.cost_stress_status is not None
    ]
    if cost_rows:
        statuses = [row.cost_stress_status for row in cost_rows]
        if any(status == "fail" for status in statuses):
            failed.append("fees_cost_sensitivity")
        elif any(status is None for status in statuses):
            missing.append("fees_cost_sensitivity")
        else:
            passed.append("fees_cost_sensitivity")
    elif any(row.fees is not None for row in rows):
        passed.append("fees_cost_sensitivity")
    else:
        missing.append("fees_cost_sensitivity")

    degradation = _holdout_degradation(rows)
    split_names = {row.evidence_split for row in rows}
    if "train" in split_names or "holdout" in split_names:
        if degradation is None:
            missing.append("train_holdout_degradation")
        elif degradation > policy.maximum_holdout_degradation_percentage:
            failed.append("train_holdout_degradation")
        else:
            passed.append("train_holdout_degradation")

    if failed:
        verdict = Verdict.REJECT
    elif missing:
        verdict = Verdict.INCONCLUSIVE
    else:
        verdict = Verdict.PASS
    return GateDecision(
        verdict=verdict,
        passed=tuple(passed),
        failed=tuple(failed),
        missing=tuple(missing),
        holdout_degradation_percentage=degradation,
    )


def _minimum_gate(
    name: str,
    rows: tuple[NormalizedEvidence, ...],
    field: str,
    threshold: float,
    passed: list[str],
    failed: list[str],
    missing: list[str],
    *,
    strict: bool = False,
) -> None:
    values = [getattr(row, field) for row in rows]
    if not values or any(value is None for value in values):
        missing.append(name)
    elif any(
        value <= threshold if strict else value < threshold
        for value in values
    ):
        failed.append(name)
    else:
        passed.append(name)


def _maximum_gate(
    name: str,
    rows: tuple[NormalizedEvidence, ...],
    field: str,
    threshold: float,
    passed: list[str],
    failed: list[str],
    missing: list[str],
) -> None:
    values = [getattr(row, field) for row in rows]
    if not values or any(value is None for value in values):
        missing.append(name)
    elif any(value > threshold for value in values):
        failed.append(name)
    else:
        passed.append(name)


def _holdout_degradation(
    rows: tuple[NormalizedEvidence, ...],
) -> float | None:
    train = [
        row.net_profit_percentage for row in rows
        if row.evidence_split == "train"
        and row.net_profit_percentage is not None
    ]
    holdout = [
        row.net_profit_percentage for row in rows
        if row.evidence_split == "holdout"
        and row.net_profit_percentage is not None
    ]
    if not train or not holdout:
        return None
    train_mean = sum(train) / len(train)
    holdout_mean = sum(holdout) / len(holdout)
    if train_mean == 0:
        return None
    return max(0.0, (train_mean - holdout_mean) / abs(train_mean) * 100)
