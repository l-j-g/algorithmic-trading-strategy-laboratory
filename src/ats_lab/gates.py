"""Deterministic research gates over the canonical evidence contract."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import ceil
from typing import Any, Iterable, Mapping

from .evidence import LifecycleStage, NormalizedEvidence
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
            parts.append("validation, robustness, and cost-stress evidence passed")
        return "Promotion evidence: " + "; ".join(parts) + "."


def evaluate_promotion(
    evidence: Iterable[NormalizedEvidence],
    *,
    policy: ResourcePolicy,
) -> PromotionDecision:
    """Require explicit validation and robustness evidence for promotion.

    This is intentionally separate from :func:`evaluate_gates`: a profitable
    baseline remains valid research evidence and should still reach analysis,
    while a paper-trade claim must survive both OOS and rolling validation,
    candles-routed Monte Carlo/path checks, and fee stress.  Lifecycle labels
    alone never satisfy the robustness gate.
    """
    rows = tuple(evidence)
    failed: list[str] = []
    missing: list[str] = []

    oos = tuple(
        row for row in rows
        if _value(row, "evidence_split") == "oos"
        or (
            _value(row, "lifecycle_stage") == "out_of_sample"
            and _value(row, "evidence_split") in {None, "oos"}
        )
    )
    rolling = tuple(
        row for row in rows if _value(row, "evidence_split") == "rolling"
    )
    training = tuple(
        row for row in rows
        if _value(row, "evidence_split") in {"train", "holdout"}
    )
    overlapping = tuple(
        row for row in oos if _overlaps_training(row, training)
    )
    if overlapping:
        failed.append("oos_training_overlap")
    eligible_oos = tuple(row for row in oos if row not in overlapping)
    _evaluate_validation_lane(
        "oos_validation", eligible_oos, policy, failed, missing,
    )
    _evaluate_validation_lane(
        "walk_forward", rolling, policy, failed, missing,
    )

    monte_carlo = tuple(
        row for row in rows
        if (
            isinstance(_value(row, "lifecycle_stage"), LifecycleStage)
            and _value(row, "lifecycle_stage") is LifecycleStage.MONTE_CARLO
        )
    )
    if not monte_carlo:
        missing.append("candles_based_monte_carlo_path_robustness")
    else:
        robustness = [
            _robustness_state(row, policy) for row in monte_carlo
        ]
        if any(state == "failed" for state in robustness):
            failed.append("candles_based_monte_carlo_path_robustness")
        elif any(state == "missing" for state in robustness):
            missing.append("candles_based_monte_carlo_path_robustness")
        else:
            # A typed Monte Carlo stage is not enough by itself.  The helper
            # requires a complete candle route and numeric path metrics.
            pass
        methods = [_value(row, "monte_carlo_method") for row in monte_carlo]
        if any(method is None for method in methods):
            missing.append("monte_carlo_method")
        elif any(method != "candle_based" for method in methods):
            failed.append("monte_carlo_method")
        scenarios = [_value(row, "monte_carlo_scenarios") for row in monte_carlo]
        if any(value is None for value in scenarios):
            missing.append("monte_carlo_scenarios")
        elif any(value < policy.monte_carlo_scenarios for value in scenarios):
            failed.append("monte_carlo_scenarios")

    walk_forward = tuple(
        row for row in rows
        if _value(row, "evidence_split") == "rolling"
        and _value(row, "walk_forward_method") in {"walk_forward", "rolling"}
    )
    if not walk_forward:
        missing.append("walk_forward_protocol")
    else:
        windows = [_value(row, "walk_forward_windows") for row in walk_forward]
        if any(value is None for value in windows):
            missing.append("walk_forward_windows")
        elif any(value < 2 for value in windows):
            failed.append("walk_forward_windows")

    cost_rows = tuple(
        row for row in rows
        if row.lifecycle_stage == "cost_sensitivity"
    )
    if not cost_rows:
        missing.append("fees_cost_sensitivity")
    else:
        states = [_robustness_state(row, policy) for row in cost_rows]
        if any(state == "failed" for state in states):
            failed.append("fees_cost_sensitivity")
        elif any(state == "missing" for state in states):
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


@dataclass(frozen=True)
class HpoCandidateDecision:
    """Deterministic gate for claims that a baseline justifies an HPO cycle.

    Mirrors the documented protocol criteria: positive baseline after fees,
    activity floor per window, multi-window positivity, no single dominant
    route, and fee sensitivity that does not destroy the edge. Missing
    evidence is inconclusive and never unlocks optimization.
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
            parts.append("hpo-candidate criteria passed")
        return "HPO-candidate evidence: " + "; ".join(parts) + "."


def evaluate_hpo_candidate(
    evidence: Iterable[NormalizedEvidence],
    *,
    policy: ResourcePolicy,
) -> HpoCandidateDecision:
    """Enforce the documented HPO-candidate criteria over canonical evidence."""
    rows = tuple(evidence)
    failed: list[str] = []
    missing: list[str] = []

    profit_rows = [
        row for row in rows if row.net_profit_percentage is not None
    ]
    if not profit_rows or any(row.fees is None for row in rows):
        missing.append("hpo_baseline_positive_after_fees")
    elif not any(row.net_profit_percentage > 0 for row in profit_rows):
        failed.append("hpo_baseline_positive_after_fees")

    if not rows or any(row.trade_count is None for row in rows):
        missing.append("hpo_activity_floor")
    elif any(
        int(row.trade_count) < required_trade_count(row, policy)
        for row in rows
    ):
        failed.append("hpo_activity_floor")

    window_groups: dict[tuple[str, str], list[float]] = {}
    for row in rows:
        if (
            row.start_date and row.finish_date
            and row.net_profit_percentage is not None
        ):
            window_groups.setdefault(
                (str(row.start_date), str(row.finish_date)), [],
            ).append(float(row.net_profit_percentage))
    positive_windows = [
        window for window, values in window_groups.items()
        if sum(values) / len(values) >= 0
    ]
    if len(window_groups) < 2:
        missing.append("hpo_multi_window_positivity")
    elif len(positive_windows) < 2:
        failed.append("hpo_multi_window_positivity")

    route_groups: dict[tuple[str, str], float] = {}
    for row in profit_rows:
        key = (str(row.symbol or ""), str(row.timeframe or ""))
        route_groups[key] = (
            route_groups.get(key, 0.0) + float(row.net_profit_percentage)
        )
    if len(route_groups) < 2:
        missing.append("hpo_single_route_dominance")
    else:
        positive_total = sum(
            value for value in route_groups.values() if value > 0
        )
        best_route = max(route_groups.values())
        if (
            positive_total > 0
            and best_route / positive_total * 100
            > policy.hpo_route_dominance_percentage
        ):
            failed.append("hpo_single_route_dominance")

    cost_rows = tuple(
        row for row in rows
        if row.lifecycle_stage == "cost_sensitivity"
    )
    if not cost_rows:
        missing.append("fees_cost_sensitivity")
    else:
        states = [_robustness_state(row, policy) for row in cost_rows]
        if any(state == "failed" for state in states):
            failed.append("fees_cost_sensitivity")
        elif any(state == "missing" for state in states):
            missing.append("fees_cost_sensitivity")

    failed = list(dict.fromkeys(failed))
    missing = list(dict.fromkeys(missing))
    return HpoCandidateDecision(
        allowed=not failed and not missing,
        failed=tuple(failed),
        missing=tuple(missing),
    )


def _overlaps_training(row: object, training_rows: Iterable[object]) -> bool:
    """Return whether one OOS row's date range intersects a training route.

    Windows are half-open [start, finish) per instrument and timeframe, so
    adjacent splits share no candle days. Undated rows cannot prove
    disjointness and are handled by the lane's route-completeness gate.
    """
    start = _value(row, "start_date")
    finish = _value(row, "finish_date")
    if not start or not finish:
        return False
    try:
        oos_start = date.fromisoformat(str(start))
        oos_finish = date.fromisoformat(str(finish))
    except ValueError:
        return False
    identity = (_value(row, "symbol"), _value(row, "timeframe"))
    for other in training_rows:
        if (
            (_value(other, "symbol"), _value(other, "timeframe")) != identity
        ):
            continue
        other_start = _value(other, "start_date")
        other_finish = _value(other, "finish_date")
        if not other_start or not other_finish:
            continue
        try:
            train_start = date.fromisoformat(str(other_start))
            train_finish = date.fromisoformat(str(other_finish))
        except ValueError:
            continue
        if oos_start < train_finish and train_start < oos_finish:
            return True
    return False


def _value(row: object, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _has_route(row: object) -> bool:
    return all(
        _value(row, name) not in (None, "")
        for name in ("symbol", "timeframe", "start_date", "finish_date")
    )


def _has_execution_provenance(row: object) -> bool:
    return all(
        _value(row, name) not in (None, "")
        for name in ("run_id", "session_id")
    )


def _has_core_metrics(row: object) -> bool:
    return all(
        _value(row, name) is not None
        for name in (
            "net_profit_percentage", "max_drawdown_percentage",
            "sharpe_ratio", "sortino_ratio", "calmar_ratio",
            "profit_factor", "trade_count",
        )
    )


def _evaluate_validation_lane(
    name: str,
    rows: tuple[object, ...],
    policy: ResourcePolicy,
    failed: list[str],
    missing: list[str],
) -> None:
    if not rows or any(not _has_route(row) for row in rows):
        missing.append(name)
        return
    quality = evaluate_gates(rows, policy=policy)  # type: ignore[arg-type]
    failed.extend(quality.failed)
    missing.extend(
        gate for gate in quality.missing
        if gate != "fees_cost_sensitivity"
    )


def _robustness_state(row: object, policy: ResourcePolicy) -> str:
    """Classify concrete Monte Carlo/path evidence without reading prose."""
    if (
        not _has_route(row)
        or not _has_execution_provenance(row)
        or not _has_core_metrics(row)
    ):
        return "missing"
    if _value(row, "verdict") in {"reject", "infrastructure_failure"}:
        return "failed"
    if (
        float(_value(row, "net_profit_percentage")) <= 0
        or float(_value(row, "max_drawdown_percentage")) > policy.maximum_drawdown_percentage
        or float(_value(row, "sharpe_ratio")) < policy.minimum_sharpe_ratio
        or float(_value(row, "profit_factor")) < policy.minimum_profit_factor
        or int(_value(row, "trade_count")) < required_trade_count(row, policy)
    ):
        return "failed"
    return "passed"


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

    _minimum_trade_gate(rows, policy, passed, failed, missing)
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
    ]
    if not cost_rows:
        missing.append("fees_cost_sensitivity")
    else:
        states = [_robustness_state(row, policy) for row in cost_rows]
        if any(state == "failed" for state in states):
            failed.append("fees_cost_sensitivity")
        elif any(state == "missing" for state in states):
            missing.append("fees_cost_sensitivity")
        else:
            passed.append("fees_cost_sensitivity")

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


def required_trade_count(row: object, policy: ResourcePolicy) -> int:
    """Return a window-normalized trade floor for one evidence row."""
    start = _value(row, "start_date")
    finish = _value(row, "finish_date")
    if not start or not finish:
        return int(policy.minimum_trades)
    try:
        days = max(
            1,
            (
                date.fromisoformat(str(finish))
                - date.fromisoformat(str(start))
            ).days,
        )
    except ValueError:
        return int(policy.minimum_trades)
    annualized = ceil(int(policy.minimum_trades_per_year) * days / 365.25)
    return max(int(policy.minimum_trade_floor), annualized)


def _minimum_trade_gate(
    rows: tuple[NormalizedEvidence, ...],
    policy: ResourcePolicy,
    passed: list[str],
    failed: list[str],
    missing: list[str],
) -> None:
    values = [row.trade_count for row in rows]
    if not values or any(value is None for value in values):
        missing.append("minimum_trades")
    elif any(
        int(value) < required_trade_count(row, policy)
        for row, value in zip(rows, values)
    ):
        failed.append("minimum_trades")
    else:
        passed.append("minimum_trades")


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
