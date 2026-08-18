from __future__ import annotations

import unittest

from ats_lab.portfolio import PortfolioCandidate, assess_portfolio


class PortfolioTests(unittest.TestCase):
    def test_flags_high_correlation_and_capacity(self) -> None:
        result = assess_portfolio(
            [
                PortfolioCandidate(
                    "A", (1.0, 2.0, 3.0), capacity_notional=100,
                    proposed_allocation_notional=80,
                ),
                PortfolioCandidate(
                    "B", (2.0, 4.0, 6.0), capacity_notional=100,
                    proposed_allocation_notional=20,
                ),
            ],
        )
        self.assertFalse(result.ready_for_portfolio_review)
        self.assertIn("high_correlation", result.blocking_reasons)
        self.assertIn("capacity_utilization_exceeded", result.blocking_reasons)

    def test_missing_series_does_not_get_called_diversified(self) -> None:
        result = assess_portfolio([
            PortfolioCandidate("A", (1.0, 2.0)),
            PortfolioCandidate("B"),
        ])
        self.assertIn("correlation_data_incomplete", result.blocking_reasons)

    def test_unrelated_series_can_pass_without_capacity_claim(self) -> None:
        result = assess_portfolio([
            PortfolioCandidate("A", (1.0, -1.0, 1.0)),
            PortfolioCandidate("B", (-1.0, -1.0, 1.0)),
        ])
        self.assertTrue(result.ready_for_portfolio_review)


if __name__ == "__main__":
    unittest.main()
