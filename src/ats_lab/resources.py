"""Local resource policy: spend machine compute while conserving agent turns."""
from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ResourcePolicy:
    mode: str = "balanced"
    cpu_cores: int = 4
    significance_simulations: int = 2000
    hpo_trials_per_parameter: int = 100
    hpo_best_candidates: int = 20
    monte_carlo_scenarios: int = 200
    synthesis_inspect_limit: int = 25
    synthesis_generate_limit: int = 25
    synthesis_low_watermark: int = 5
    synthesis_min_new_concepts: int = 5
    synthesis_max_improvements: int = 20
    synthesis_retry_cooldown_seconds: int = 300
    synthesis_lease_seconds: int = 3600
    active_ready_limit: int = 3

    def __post_init__(self) -> None:
        if self.mode not in {"balanced", "compute_heavy"}:
            raise ValueError("resources.mode must be balanced or compute_heavy")
        for name in (
            "cpu_cores", "significance_simulations", "hpo_trials_per_parameter",
            "hpo_best_candidates", "monte_carlo_scenarios", "synthesis_inspect_limit",
            "synthesis_generate_limit", "synthesis_low_watermark",
            "synthesis_min_new_concepts", "synthesis_max_improvements",
            "synthesis_retry_cooldown_seconds", "synthesis_lease_seconds",
            "active_ready_limit",
        ):
            if int(getattr(self, name)) < (0 if name == "synthesis_low_watermark" else 1):
                raise ValueError(f"resources.{name} must be non-negative" if name == "synthesis_low_watermark"
                                 else f"resources.{name} must be positive")
        if self.significance_simulations < 2000:
            raise ValueError("resources.significance_simulations must be at least 2000")
        if self.synthesis_generate_limit > self.synthesis_inspect_limit:
            raise ValueError("resources.synthesis_generate_limit cannot exceed inspect limit")
        if self.synthesis_low_watermark >= self.synthesis_generate_limit:
            raise ValueError("resources.synthesis_low_watermark must be below generation limit")
        if self.synthesis_min_new_concepts + self.synthesis_max_improvements != self.synthesis_generate_limit:
            raise ValueError(
                "resources.synthesis_min_new_concepts + synthesis_max_improvements "
                "must equal synthesis_generate_limit"
            )

    def to_dict(self) -> dict:
        return asdict(self)


def load_resource_policy(config_path: Path) -> ResourcePolicy:
    if not config_path.is_file():
        return ResourcePolicy()
    with config_path.open("rb") as handle:
        payload = tomllib.load(handle).get("resources", {})
    return ResourcePolicy(**payload)
