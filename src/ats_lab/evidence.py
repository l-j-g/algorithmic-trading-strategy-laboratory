"""Canonical normalized research evidence shared by every operator surface."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping

from .models import Verdict


class EvidenceSplit(StrEnum):
    TRAIN = "train"
    HOLDOUT = "holdout"
    OOS = "oos"
    ROLLING = "rolling"


class LifecycleStage(StrEnum):
    BASELINE = "baseline"
    MULTI_WINDOW = "multi_window"
    COST_SENSITIVITY = "cost_sensitivity"
    OUT_OF_SAMPLE = "out_of_sample"
    SIGNIFICANCE = "significance"
    MONTE_CARLO = "monte_carlo"
    HPO = "hpo"
    HARNESS_CHECK = "harness_check"
    PAPER_TRADE = "paper_trade"


class CostStressStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class NormalizedEvidence:
    schema_version: int = 2
    strategy: str | None = None
    strategy_version: str | None = None
    lifecycle_stage: LifecycleStage | None = None
    verdict: Verdict | None = None
    experiment_id: str = ""
    run_id: str | None = None
    session_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    start_date: str | None = None
    finish_date: str | None = None
    evidence_split: EvidenceSplit | None = None
    net_profit_percentage: float | None = None
    max_drawdown_percentage: float | None = None
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None
    profit_factor: float | None = None
    win_rate: float | None = None
    trade_count: int | None = None
    fees: float | None = None
    expectancy: float | None = None
    leverage: float | None = None
    leverage_mode: str | None = None
    configured_futures_leverage: float | None = None
    effective_leverage_mean: float | None = None
    effective_leverage_p95: float | None = None
    effective_leverage_max: float | None = None
    liquidation_count: int | None = None
    risk_per_trade_percentage: float | None = None
    optimizer_objective: str | None = None
    cost_stress_status: CostStressStatus | None = None
    significance_p_value: float | None = None
    monte_carlo_scenarios: int | None = None
    monte_carlo_method: str | None = None
    walk_forward_windows: int | None = None
    walk_forward_method: str | None = None
    completed_at: str | None = None
    finding: str | None = None
    next_action: str | None = None

    def __post_init__(self) -> None:
        if not self.experiment_id:
            raise ValueError("experiment_id is required")

    def to_dict(self) -> dict[str, Any]:
        """Stable full serialization. Missing values remain null."""
        payload = asdict(self)
        for field in (
            "lifecycle_stage", "verdict", "evidence_split", "cost_stress_status",
        ):
            value = getattr(self, field)
            payload[field] = value.value if value is not None else None
        return payload

    def to_compact_dict(self) -> dict[str, Any]:
        """Token-efficient serialization using canonical names only."""
        return {
            key: value for key, value in self.to_dict().items()
            if value is not None
        }

    def to_compact_json(self) -> str:
        return json.dumps(
            self.to_compact_dict(), sort_keys=True, separators=(",", ":"),
        )

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> NormalizedEvidence:
        available = set(row.keys())
        values = {key: row[key] for key in _FIELD_NAMES if key in available}
        values["lifecycle_stage"] = _enum_or_none(
            LifecycleStage, values.get("lifecycle_stage"),
        )
        values["verdict"] = _enum_or_none(Verdict, values.get("verdict"))
        values["evidence_split"] = _enum_or_none(
            EvidenceSplit, values.get("evidence_split"),
        )
        values["cost_stress_status"] = _enum_or_none(
            CostStressStatus, values.get("cost_stress_status"),
        )
        return cls(**values)


# CandidateMetrics is another domain name for the exact same contract.
CandidateMetrics = NormalizedEvidence

_FIELD_NAMES = tuple(NormalizedEvidence.__dataclass_fields__)
_CHILD_COLLECTIONS = ("route_runs", "atomic_routes", "routes", "route_results")

# Per-route outcome aggregates. When one raw run expands into multiple atomic
# route rows, these keys never inherit from the parent metric payload: a route
# row carries an outcome only if its own metrics declare it. Configuration and
# provenance fields (leverage, strategy version, protocol metadata, ...) still
# inherit so shared setup is not duplicated per route.
_ROUTE_OUTCOME_FIELDS = frozenset({
    "net_profit_percentage",
    "max_drawdown_percentage",
    "sharpe_ratio",
    "sortino_ratio",
    "calmar_ratio",
    "profit_factor",
    "win_rate",
    "trade_count",
    "fees",
    "expectancy",
    "effective_leverage_mean",
    "effective_leverage_p95",
    "effective_leverage_max",
    "liquidation_count",
    "significance_p_value",
})

_METRIC_ALIASES = {
    "net_profit_percentage": (
        "net_profit_percentage", "net_profit_pct", "net_profit_percent",
    ),
    "max_drawdown_percentage": (
        "max_drawdown_percentage", "max_drawdown_pct", "max_drawdown",
    ),
    "sharpe_ratio": ("sharpe_ratio", "sharpe"),
    "sortino_ratio": ("sortino_ratio", "sortino"),
    "calmar_ratio": ("calmar_ratio", "calmar"),
    "profit_factor": ("profit_factor", "gross_profit_loss_ratio"),
    "trade_count": ("trade_count", "total_trades", "total", "trades"),
    "fees": ("fees", "total_fees", "fee"),
    "expectancy": ("expectancy",),
    "leverage_mode": (
        "leverage_mode", "futures_leverage_mode", "margin_mode", "leverage_type",
        "mode",
    ),
    "leverage": ("leverage",),
    "configured_futures_leverage": (
        "configured_futures_leverage", "configured_leverage", "futures_leverage",
    ),
    "effective_leverage_mean": (
        "effective_leverage_mean", "mean_effective_leverage",
        "effective_leverage_avg", "average_effective_leverage", "mean_leverage",
        "average_leverage", "avg_leverage", "effective_leverage",
    ),
    "effective_leverage_p95": (
        "effective_leverage_p95", "effective_leverage_95p",
        "effective_leverage_95th_percentile", "effective_leverage_percentile_95",
        "leverage_p95", "p95_leverage",
    ),
    "effective_leverage_max": (
        "effective_leverage_max", "max_effective_leverage",
        "effective_leverage_peak", "max_leverage", "leverage_max",
    ),
    "liquidation_count": (
        "liquidation_count", "liquidations", "total_liquidations",
        "number_of_liquidations", "num_liquidations", "liquidation_events",
    ),
    "risk_per_trade_percentage": (
        "risk_per_trade_percentage", "risk_per_trade_pct", "risk_per_trade",
    ),
    "significance_p_value": ("significance_p_value", "p_value"),
}

_ROUTE_OUTCOME_KEYS = frozenset(
    alias
    for field, aliases in _METRIC_ALIASES.items()
    if field in _ROUTE_OUTCOME_FIELDS
    for alias in aliases
) | {"gross_profit", "gross_loss", "win_rate", "win_rate_percentage"}


def display_value(value: object) -> str:
    """Common UI/CLI missing-value representation."""
    return "—" if value is None else str(value)


def evidence_key(evidence: NormalizedEvidence) -> str:
    identity = (
        evidence.experiment_id,
        evidence.run_id,
        evidence.session_id,
        evidence.symbol,
        evidence.timeframe,
        evidence.start_date,
        evidence.finish_date,
        evidence.evidence_split.value if evidence.evidence_split else None,
    )
    encoded = json.dumps(identity, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def normalize_run_evidence(
    *,
    experiment_id: str,
    run_id: str | None,
    session_id: str | None,
    strategy: str | None,
    lifecycle_stage: str | LifecycleStage | None,
    experiment_spec: Mapping[str, Any] | None,
    route: Mapping[str, Any] | None,
    metrics: Mapping[str, Any] | None,
    completed_at: str | None,
    verdict: str | Verdict | None = None,
    finding: str | None = None,
    next_action: str | None = None,
) -> tuple[NormalizedEvidence, ...]:
    """Expand one raw run into one canonical row per atomic route.

    Win-rate units are declared by the metric key, never inferred from
    magnitude: ``win_rate_percentage`` is stored verbatim as a percentage and
    ``win_rate`` is a fraction in ``[0, 1]`` scaled by 100. A ``win_rate``
    outside that range violates the declared unit contract and normalizes to
    null instead of being guessed.
    """
    spec = dict(experiment_spec or {})
    raw_metrics = dict(metrics or {})
    parent_route = dict(route or {})
    children: list[Mapping[str, Any]] | None = None
    for name in _CHILD_COLLECTIONS:
        value = raw_metrics.get(name)
        if isinstance(value, list) and any(isinstance(item, Mapping) for item in value):
            children = [item for item in value if isinstance(item, Mapping)]
            break
    has_route_children = children is not None
    if children is None:
        children = ({},)

    result = []
    for child in children:
        child_metrics_value = child.get("metrics")
        child_metrics = (
            dict(child_metrics_value)
            if isinstance(child_metrics_value, Mapping)
            else dict(child)
        )
        if has_route_children:
            parent_context = {
                key: value for key, value in raw_metrics.items()
                if key not in _ROUTE_OUTCOME_KEYS
            }
            merged_metrics = {**parent_context, **child_metrics}
        else:
            merged_metrics = dict(raw_metrics)
        child_route_value = child.get("route")
        child_route = (
            dict(child_route_value)
            if isinstance(child_route_value, Mapping)
            else {
                key: child[key]
                for key in ("exchange", "symbol", "timeframe", "start_date", "finish_date")
                if key in child
            }
        )
        merged_route = {**parent_route, **child_route}
        result.append(_normalize_atomic(
            experiment_id=experiment_id,
            run_id=run_id,
            session_id=_text(child.get("session_id")) or _text(session_id),
            strategy=strategy,
            lifecycle_stage=lifecycle_stage,
            spec=spec,
            route=merged_route,
            metrics=merged_metrics,
            completed_at=_text(child.get("completed_at"))
            or _text(child.get("finished_at"))
            or _text(completed_at),
            verdict=verdict,
            finding=finding,
            next_action=next_action,
        ))
    return tuple(result)


def _normalize_atomic(
    *,
    experiment_id: str,
    run_id: str | None,
    session_id: str | None,
    strategy: str | None,
    lifecycle_stage: str | LifecycleStage | None,
    spec: Mapping[str, Any],
    route: Mapping[str, Any],
    metrics: Mapping[str, Any],
    completed_at: str | None,
    verdict: str | Verdict | None,
    finding: str | None,
    next_action: str | None,
) -> NormalizedEvidence:
    stage = _enum_or_none(LifecycleStage, lifecycle_stage)
    split_value = _first(
        metrics, route, spec,
        names=("evidence_split", "split", "data_split"),
    )
    if split_value is None and stage is LifecycleStage.OUT_OF_SAMPLE:
        split_value = EvidenceSplit.OOS.value

    numeric: dict[str, float | int | None] = {}
    for field, aliases in _METRIC_ALIASES.items():
        raw = _first(metrics, spec, names=aliases)
        numeric[field] = (
            _integer(raw)
            if field in {"trade_count", "liquidation_count"}
            else _number(raw)
        )
    if numeric["profit_factor"] is None:
        gross_profit = _number(metrics.get("gross_profit"))
        gross_loss = _number(metrics.get("gross_loss"))
        if gross_profit is not None and gross_loss not in (None, 0):
            numeric["profit_factor"] = gross_profit / abs(gross_loss)
    if numeric["max_drawdown_percentage"] is not None:
        numeric["max_drawdown_percentage"] = abs(
            float(numeric["max_drawdown_percentage"]),
        )

    win_rate_percentage = _number(metrics.get("win_rate_percentage"))
    raw_win_rate = _number(metrics.get("win_rate"))
    if win_rate_percentage is not None:
        win_rate = win_rate_percentage
    elif raw_win_rate is None:
        win_rate = None
    elif 0 <= raw_win_rate <= 1:
        win_rate = raw_win_rate * 100
    else:
        win_rate = None

    cost_status = _normalize_cost_status(_first(
        metrics, spec, names=("cost_stress_status",),
    ))
    objective = _text(_first(
        metrics, spec, names=("optimizer_objective", "objective"),
    ))
    leverage_mode = _protocol_text(_first(
        metrics, spec, names=_METRIC_ALIASES["leverage_mode"],
    ))
    monte_carlo_method = _protocol_text(_first(
        metrics, spec,
        names=("monte_carlo_method", "simulation_method", "resampling_method"),
    ))
    if monte_carlo_method is None and metrics.get("candle_based") is True:
        monte_carlo_method = "candle_based"
    monte_carlo_scenarios = _integer(_first(
        metrics, spec,
        names=("monte_carlo_scenarios", "n_scenarios", "scenario_count"),
    ))
    walk_forward_windows = _integer(_first(
        metrics, spec,
        names=("walk_forward_windows", "n_walk_forward_windows", "rolling_windows"),
    ))
    walk_forward_method = _protocol_text(_first(
        metrics, spec,
        names=("walk_forward_method", "validation_method", "window_method"),
    ))
    return NormalizedEvidence(
        strategy=_text(strategy),
        strategy_version=_text(_first(
            metrics, spec, names=("strategy_version",),
        )),
        lifecycle_stage=stage,
        verdict=_enum_or_none(Verdict, verdict),
        experiment_id=experiment_id,
        run_id=_text(run_id),
        session_id=session_id,
        symbol=_text(route.get("symbol")) or _text(metrics.get("symbol")),
        timeframe=_text(route.get("timeframe")) or _text(metrics.get("timeframe")),
        start_date=_text(route.get("start_date")) or _text(metrics.get("start_date")),
        finish_date=_text(route.get("finish_date")) or _text(metrics.get("finish_date")),
        evidence_split=_enum_or_none(EvidenceSplit, split_value),
        net_profit_percentage=numeric["net_profit_percentage"],
        max_drawdown_percentage=numeric["max_drawdown_percentage"],
        sharpe_ratio=numeric["sharpe_ratio"],
        sortino_ratio=numeric["sortino_ratio"],
        calmar_ratio=numeric["calmar_ratio"],
        profit_factor=numeric["profit_factor"],
        win_rate=win_rate,
        trade_count=numeric["trade_count"],
        fees=numeric["fees"],
        expectancy=numeric["expectancy"],
        leverage=numeric["leverage"],
        leverage_mode=leverage_mode,
        configured_futures_leverage=numeric["configured_futures_leverage"],
        effective_leverage_mean=numeric["effective_leverage_mean"],
        effective_leverage_p95=numeric["effective_leverage_p95"],
        effective_leverage_max=numeric["effective_leverage_max"],
        liquidation_count=numeric["liquidation_count"],
        risk_per_trade_percentage=numeric["risk_per_trade_percentage"],
        optimizer_objective=objective,
        cost_stress_status=cost_status,
        significance_p_value=numeric["significance_p_value"],
        monte_carlo_scenarios=monte_carlo_scenarios,
        monte_carlo_method=monte_carlo_method,
        walk_forward_windows=walk_forward_windows,
        walk_forward_method=walk_forward_method,
        completed_at=_text(completed_at),
        finding=_text(finding),
        next_action=_text(next_action),
    )


def _first(
    *payloads: Mapping[str, Any],
    names: tuple[str, ...],
) -> Any:
    for payload in payloads:
        for name in names:
            if name in payload and payload[name] is not None:
                return payload[name]
    return None


def _protocol_text(value: object) -> str | None:
    text = _text(value)
    if not text:
        return None
    normalized = text.casefold().replace("-", "_").replace(" ", "_")
    if normalized in {"candle", "candles_based", "candle_resampled", "candle_data"}:
        return "candle_based"
    return normalized


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _enum_or_none(enum: type[StrEnum], value: object) -> Any:
    if value is None:
        return None
    raw = value.value if isinstance(value, StrEnum) else str(value)
    raw = raw.strip().lower().replace("-", "_")
    if not raw or raw == "unknown":
        return None
    try:
        return enum(raw)
    except ValueError:
        return None


def _normalize_cost_status(value: object) -> CostStressStatus | None:
    raw = _text(value)
    aliases = {
        "passed": "pass",
        "failed": "fail",
        "rejected": "fail",
    }
    return _enum_or_none(CostStressStatus, aliases.get(raw or "", raw))
