"""Local resource policy: spend machine compute while conserving agent turns."""
from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path


@dataclass(frozen=True)
class EvaluationWindowPolicy:
    """Configurable window defaults resolved into explicit route dates.

    Relative windows are convenient for recurring research.  The resolved
    dates must still be copied into each :class:`RouteSpec` so a run remains
    reproducible after the policy or its anchor date changes.  ``explicit``
    disables default generation and leaves route dates entirely to callers.
    """

    mode: str = "relative"
    as_of_date: str | None = None
    comparison_lookback_days: int = 365
    oos_lookback_days: int = 180
    rolling_lookback_days: int = 90

    def __post_init__(self) -> None:
        if self.mode not in {"relative", "explicit"}:
            raise ValueError(
                "resources.evaluation_windows.mode must be relative or explicit"
            )
        if self.as_of_date is not None:
            try:
                date.fromisoformat(self.as_of_date)
            except ValueError as error:
                raise ValueError(
                    "resources.evaluation_windows.as_of_date must be ISO date"
                ) from error
        for name in (
            "comparison_lookback_days", "oos_lookback_days",
            "rolling_lookback_days",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(
                    f"resources.evaluation_windows.{name} must be positive"
                )

    def resolve(self, *, as_of: date | str | None = None) -> dict[str, dict[str, str]]:
        """Resolve relative defaults; explicit mode returns no defaults."""
        if self.mode == "explicit":
            return {}
        if as_of is None:
            anchor = (
                date.fromisoformat(self.as_of_date)
                if self.as_of_date is not None else date.today()
            )
        elif isinstance(as_of, date):
            anchor = as_of
        else:
            anchor = date.fromisoformat(as_of)

        def window(lookback_days: int) -> dict[str, str]:
            return {
                "start_date": (anchor - timedelta(days=lookback_days)).isoformat(),
                "finish_date": anchor.isoformat(),
            }

        return {
            "comparison": window(self.comparison_lookback_days),
            "oos": window(self.oos_lookback_days),
            "rolling": window(self.rolling_lookback_days),
        }


@dataclass(frozen=True)
class ResourcePolicy:
    mode: str = "balanced"
    cpu_cores: int = 4
    significance_simulations: int = 2000
    significance_fdr_level: float = 0.05
    hpo_trials_per_parameter: int = 100
    hpo_best_candidates: int = 20
    hpo_route_dominance_percentage: float = 50.0
    monte_carlo_scenarios: int = 500
    synthesis_inspect_limit: int = 25
    synthesis_generate_limit: int = 25
    synthesis_low_watermark: int = 5
    synthesis_min_new_concepts: int = 5
    synthesis_max_improvements: int = 20
    synthesis_max_revision_depth: int = 3
    synthesis_failure_diagnosis_limit: int = 8
    synthesis_retry_cooldown_seconds: int = 300
    synthesis_lease_seconds: int = 3600
    claim_timeout_seconds: int = 7200
    execution_batch_size: int = 8
    active_ready_limit: int = 3
    analysis_cohort_min: int = 4
    analysis_cohort_max: int = 8
    analysis_parallelism: int = 1
    analyzer_timeout_seconds: int = 900
    analyzer_retry_limit: int = 1
    minimum_trades: int = 50
    minimum_trades_per_year: int = 20
    minimum_trade_floor: int = 12
    maximum_drawdown_percentage: float = 30.0
    minimum_sharpe_ratio: float = 0.0
    minimum_profit_factor: float = 1.0
    maximum_holdout_degradation_percentage: float = 50.0
    evaluation_windows: EvaluationWindowPolicy = EvaluationWindowPolicy()
    portfolio_correlation_threshold: float = 0.85
    portfolio_capacity_utilization_limit: float = 0.70

    def __post_init__(self) -> None:
        if self.mode not in {"balanced", "compute_heavy"}:
            raise ValueError("resources.mode must be balanced or compute_heavy")
        for name in (
            "cpu_cores", "significance_simulations", "hpo_trials_per_parameter",
            "hpo_best_candidates", "monte_carlo_scenarios", "synthesis_inspect_limit",
            "synthesis_generate_limit", "synthesis_low_watermark",
            "synthesis_min_new_concepts", "synthesis_max_improvements",
            "synthesis_max_revision_depth", "synthesis_failure_diagnosis_limit",
            "synthesis_retry_cooldown_seconds", "synthesis_lease_seconds",
            "claim_timeout_seconds", "execution_batch_size", "active_ready_limit",
            "analysis_cohort_min", "analysis_cohort_max",
            "analysis_parallelism", "analyzer_timeout_seconds",
            "minimum_trades", "minimum_trades_per_year", "minimum_trade_floor",
        ):
            if int(getattr(self, name)) < (0 if name == "synthesis_low_watermark" else 1):
                raise ValueError(f"resources.{name} must be non-negative" if name == "synthesis_low_watermark"
                                 else f"resources.{name} must be positive")
        if self.significance_simulations < 2000:
            raise ValueError("resources.significance_simulations must be at least 2000")
        if not 0 < float(self.significance_fdr_level) <= 1:
            raise ValueError("resources.significance_fdr_level must be in (0, 1]")
        if self.monte_carlo_scenarios < 500:
            raise ValueError("resources.monte_carlo_scenarios must be at least 500")
        if self.synthesis_generate_limit > self.synthesis_inspect_limit:
            raise ValueError("resources.synthesis_generate_limit cannot exceed inspect limit")
        if self.synthesis_low_watermark >= self.synthesis_generate_limit:
            raise ValueError("resources.synthesis_low_watermark must be below generation limit")
        if self.synthesis_min_new_concepts + self.synthesis_max_improvements != self.synthesis_generate_limit:
            raise ValueError(
                "resources.synthesis_min_new_concepts + synthesis_max_improvements "
                "must equal synthesis_generate_limit"
            )
        if not 4 <= self.analysis_cohort_min <= self.analysis_cohort_max <= 8:
            raise ValueError(
                "resources analysis cohort must satisfy 4 <= min <= max <= 8"
            )
        if self.analysis_parallelism > 4:
            raise ValueError("resources.analysis_parallelism must be at most 4")
        if not 600 <= self.analyzer_timeout_seconds <= 900:
            raise ValueError(
                "resources.analyzer_timeout_seconds must be between 600 and 900"
            )
        if self.analyzer_retry_limit != 1:
            raise ValueError("resources.analyzer_retry_limit must equal 1")
        for name in (
            "maximum_drawdown_percentage", "minimum_profit_factor",
            "maximum_holdout_degradation_percentage",
        ):
            if float(getattr(self, name)) < 0:
                raise ValueError(f"resources.{name} must be non-negative")
        if not 0 < float(self.hpo_route_dominance_percentage) <= 100:
            raise ValueError(
                "resources.hpo_route_dominance_percentage must be in (0, 100]"
            )
        if not 0 < float(self.portfolio_correlation_threshold) <= 1:
            raise ValueError("resources.portfolio_correlation_threshold must be in (0, 1]")
        if not 0 < float(self.portfolio_capacity_utilization_limit) <= 1:
            raise ValueError(
                "resources.portfolio_capacity_utilization_limit must be in (0, 1]"
            )
    def to_dict(self) -> dict:
        return asdict(self)


def load_resource_policy(config_path: Path) -> ResourcePolicy:
    if not config_path.is_file():
        return ResourcePolicy()
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle).get("resources", {})
    windows = payload.pop("evaluation_windows", {})
    if not isinstance(windows, dict):
        raise ValueError("resources.evaluation_windows must be a table")
    payload["evaluation_windows"] = EvaluationWindowPolicy(**windows)
    return ResourcePolicy(**payload)
