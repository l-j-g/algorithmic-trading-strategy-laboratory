from __future__ import annotations

import unittest

from ats_lab.models import DataRouteSpec
from ats_lab.strategy_dependencies import (
    merge_data_routes,
    required_data_routes,
    trusted_data_route_manifest,
)


class StrategyDependencyTests(unittest.TestCase):
    def test_eth_btc_strategy_has_reviewed_btc_route(self) -> None:
        self.assertEqual(
            required_data_routes(
                "EthBtcRatioZscoreRevert",
                [{"exchange": "Binance Perpetual Futures", "timeframe": "1h"}],
            ),
            (DataRouteSpec(
                exchange="Binance Perpetual Futures",
                symbol="BTC-USDT",
                timeframe="1h",
            ),),
        )

    def test_primary_template_follows_trading_route_timeframe(self) -> None:
        self.assertEqual(
            required_data_routes(
                "EthBtcRelativeStrengthMomentum",
                [{"exchange": "Binance Perpetual Futures", "timeframe": "4h"}],
            ),
            (DataRouteSpec(
                exchange="Binance Perpetual Futures",
                symbol="BTC-USDT",
                timeframe="4h",
            ),),
        )

    def test_merge_preserves_explicit_routes_and_adds_trusted_routes(self) -> None:
        merged = merge_data_routes(
            "EthBtcRatioZscoreRevert",
            [{"exchange": "Binance Perpetual Futures", "timeframe": "1h"}],
            [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "ETH-USDT",
                "timeframe": "4h",
            }],
            [{
                "exchange": "Binance Perpetual Futures",
                "symbol": "BTC-USDT",
                "timeframe": "1h",
            }],
        )
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0].symbol, "ETH-USDT")
        self.assertEqual(merged[1].symbol, "BTC-USDT")

    def test_manifest_is_nonempty_and_versioned(self) -> None:
        manifest = trusted_data_route_manifest()
        self.assertIn("EthBtcRatioZscoreRevert", manifest)
        self.assertIn("LondonCloseExtensionFade", manifest)


if __name__ == "__main__":
    unittest.main()
