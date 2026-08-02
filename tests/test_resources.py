from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ats_lab.resources import ResourcePolicy, load_resource_policy


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
analysis_cohort_min = 4
analysis_cohort_max = 8
analysis_parallelism = 2
analyzer_timeout_seconds = 720
""")
            policy = load_resource_policy(path)
            self.assertEqual(policy.cpu_cores, 6)
            self.assertEqual(policy.significance_simulations, 5000)
            self.assertEqual(policy.hpo_trials_per_parameter, 300)
            self.assertEqual(policy.synthesis_generate_limit, 25)
            self.assertEqual(policy.synthesis_low_watermark, 5)
            self.assertEqual(policy.synthesis_min_new_concepts, 5)
            self.assertEqual(policy.analysis_parallelism, 2)
            self.assertEqual(policy.analyzer_timeout_seconds, 720)

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


if __name__ == "__main__":
    unittest.main()
