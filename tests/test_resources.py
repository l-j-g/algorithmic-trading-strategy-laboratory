from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from datetime import date

from ats_lab.resources import (
    EvaluationWindowPolicy,
    ResourcePolicy,
    load_resource_policy,
)


class ResourcePolicyTests(unittest.TestCase):
    def test_loads_compute_heavy_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("""[resources]
mode = "compute_heavy"
cpu_cores = 6
significance_simulations = 5000
hpo_trials_per_parameter = 300
hpo_best_candidates = 50
monte_carlo_scenarios = 500
synthesis_inspect_limit = 25
synthesis_generate_limit = 25
synthesis_low_watermark = 5
synthesis_min_new_concepts = 5
synthesis_max_improvements = 20
synthesis_retry_cooldown_seconds = 300
synthesis_lease_seconds = 3600
active_ready_limit = 3
execution_parallelism = 2
analysis_cohort_min = 4
analysis_cohort_max = 8
analysis_parallelism = 2
analyzer_timeout_seconds = 720

[resources.evaluation_windows]
mode = "relative"
as_of_date = "2026-08-18"
comparison_lookback_days = 365
oos_lookback_days = 180
rolling_lookback_days = 90
""")
            policy = load_resource_policy(path)
            self.assertEqual(policy.cpu_cores, 6)
            self.assertEqual(policy.significance_simulations, 5000)
            self.assertEqual(policy.hpo_trials_per_parameter, 300)
            self.assertEqual(policy.synthesis_generate_limit, 25)
            self.assertEqual(policy.synthesis_low_watermark, 5)
            self.assertEqual(policy.synthesis_min_new_concepts, 5)
            self.assertEqual(policy.execution_parallelism, 2)
            self.assertEqual(policy.analysis_parallelism, 2)
            self.assertEqual(policy.analyzer_timeout_seconds, 720)
            self.assertEqual(policy.monte_carlo_scenarios, 500)
            self.assertEqual(
                policy.evaluation_windows.resolve(),
                {
                    "comparison": {"start_date": "2025-08-18", "finish_date": "2026-08-18"},
                    "oos": {"start_date": "2026-02-19", "finish_date": "2026-08-18"},
                    "rolling": {"start_date": "2026-05-20", "finish_date": "2026-08-18"},
                },
            )

    def test_explicit_mode_preserves_route_owned_dates(self) -> None:
        policy = EvaluationWindowPolicy(mode="explicit")
        self.assertEqual(policy.resolve(as_of=date(2026, 8, 18)), {})

    def test_executor_infrastructure_failure_limit_defaults_and_parses(self) -> None:
        self.assertEqual(ResourcePolicy().executor_infrastructure_failure_limit, 10)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text(
                "[resources]\nexecutor_infrastructure_failure_limit = 4\n"
            )
            policy = load_resource_policy(path)
            self.assertEqual(policy.executor_infrastructure_failure_limit, 4)

    def test_rejects_non_positive_executor_infrastructure_failure_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            ResourcePolicy(executor_infrastructure_failure_limit=0)

    def test_rejects_too_few_significance_simulations(self) -> None:
        with self.assertRaises(ValueError):
            ResourcePolicy(significance_simulations=1000)

    def test_rejects_invalid_synthesis_lane_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "must equal synthesis_generate_limit"):
            ResourcePolicy(synthesis_min_new_concepts=6)

    def test_rejects_invalid_analyzer_bounds(self) -> None:
        with self.assertRaisesRegex(ValueError, "4 <= min <= max <= 8"):
            ResourcePolicy(analysis_cohort_min=3)
        with self.assertRaisesRegex(ValueError, "between 600 and 900"):
            ResourcePolicy(analyzer_timeout_seconds=599)

    def test_rejects_execution_parallelism_above_reserved_cpu(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not exceed cpu_cores"):
            ResourcePolicy(cpu_cores=2, execution_parallelism=3)

    def test_rejects_invalid_window_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "relative or explicit"):
            EvaluationWindowPolicy(mode="calendar")


if __name__ == "__main__":
    unittest.main()
