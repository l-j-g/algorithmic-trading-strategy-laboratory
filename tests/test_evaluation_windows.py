from __future__ import annotations

import unittest

from ats_lab.evaluation_windows import resolve_evaluation_windows
from ats_lab.resources import EvaluationWindowPolicy, ResourcePolicy


class EvaluationWindowTests(unittest.TestCase):
    def test_relative_windows_are_disjoint_and_reproducible(self) -> None:
        policy = ResourcePolicy(evaluation_windows=EvaluationWindowPolicy(
            as_of_date="2026-08-18",
            comparison_lookback_days=365,
            rolling_lookback_days=90,
            oos_lookback_days=180,
        ))
        first = resolve_evaluation_windows(policy)
        second = resolve_evaluation_windows(policy)
        self.assertEqual(first, second)
        self.assertEqual(first.hpo_finish, first.rolling_start)
        self.assertEqual(first.rolling_finish, first.oos_start)
        self.assertEqual(first.oos_finish.isoformat(), "2026-08-18")


if __name__ == "__main__":
    unittest.main()
