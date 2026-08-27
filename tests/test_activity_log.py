import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ats_lab.activity_log import (
    ActivityEvent,
    ActivityFollower,
    ActivityLogConfig,
    format_event_gap,
    format_total_duration,
    render_activity_event,
    render_footer,
    strip_terminal_controls,
)
from ats_lab.database import WorkflowDatabase


class TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


class ActivityLogTests(unittest.TestCase):
    def test_duration_and_gap_formats_are_human_readable(self) -> None:
        self.assertEqual(format_total_duration(0), "0 hrs : 00 min")
        self.assertEqual(
            format_total_duration(4 * 3600 + 24 * 60),
            "4 hrs : 24 min",
        )
        self.assertEqual(format_event_gap(55), "+55 sec")
        self.assertEqual(format_event_gap(200), "+3 min 20 sec")
        self.assertEqual(format_event_gap(3600), "+1 hr 0 min")

    def test_config_renders_date_log_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / ".ats-lab" / "config.toml"
            config_path.parent.mkdir()
            config_path.write_text(
                '[logging]\nlog_to_file = true\n'
                'log_dir = "{ats-lab}/logs/{date}_log"\n'
            )
            config = ActivityLogConfig.from_file(config_path, repo=root)
            path = config.daily_path(
                datetime(2026, 8, 27, tzinfo=timezone.utc),
            )
            self.assertEqual(path, root / ".ats-lab/logs/2026-08-27_log")

    def test_stage_renderers_keep_colons_and_persisted_context(self) -> None:
        synthesis = ActivityEvent(
            1, "supervisor", "worker", "synthesis_completed", {
                "items": [{
                    "lane": "new_concept",
                    "strategy_name": "MeanReversionStrategy",
                    "hypothesis": "Mean reversion follows volatility compression.",
                    "routes": [{"symbol": "BTC-USDT", "timeframe": "1h"}],
                }],
            }, "2026-08-27T00:00:00Z",
        )
        rendered = render_activity_event(synthesis)
        self.assertIn("SYNTHESIS:", rendered)
        self.assertIn("=> 01  NEW  MeanReversionStrategy · BTC-USDT · 1h", rendered)
        self.assertIn("↳ evaluating: Mean reversion follows volatility compression.", rendered)

        analysis = ActivityEvent(
            2, "supervisor", "worker", "analysis_completed", {
                "total": 1,
                "items": [{
                    "strategy": "MeanReversionStrategy",
                    "verdict": "pass",
                    "summary": "Lifecycle gates cleared.",
                }],
            }, "2026-08-27T00:01:00Z",
        )
        self.assertIn(
            "ANALYSIS: Completed (1/1)\n  01  PASS  MeanReversionStrategy · Lifecycle gates cleared.",
            render_activity_event(analysis),
        )

    def test_run_render_contains_metric_colours_and_jesse_link_fallback(self) -> None:
        event = ActivityEvent(
            3, "supervisor", "worker", "run_completed", {
                "operation": "backtest",
                "completed": 3,
                "total": 25,
                "strategy": "BreakoutTrendStrategy",
                "routes": [{"symbol": "ETH-USDT", "timeframe": "4h"}],
                "metrics": {
                    "trade_count": 42,
                    "net_profit_percentage": 12.35,
                    "sharpe_ratio": 1.23,
                    "max_drawdown_percentage": -4.5,
                },
                "metric_states": {
                    "trade_count": "green",
                    "net_profit_percentage": "green",
                    "sharpe_ratio": "yellow",
                    "max_drawdown_percentage": "red",
                },
                "dashboard_url": "http://127.0.0.1:9000/#/backtest/session",
            }, "2026-08-27T00:02:00Z",
        )
        plain = render_activity_event(event, links=False)
        self.assertIn("RUNNING (3/25): Backtest Complete · BreakoutTrendStrategy", plain)
        self.assertIn("trades=42 · net=+12.35% · sharpe=1.23 · max_dd=-4.50%", plain)
        self.assertIn("Jesse: http://127.0.0.1:9000/#/backtest/session", plain)
        colored = render_activity_event(event, color=True)
        self.assertIn("\033[", colored)

    def test_footer_has_total_gap_and_optional_token_meter_only(self) -> None:
        footer = render_footer(
            "WAITING", total_seconds=4 * 3600 + 24 * 60,
            since_event_seconds=200, tokens=469,
        )
        self.assertEqual(
            footer,
            "└─ WAITING          · 4 hrs : 24 min (+3 min 20 sec) · (^ 469)",
        )
        self.assertNotIn("LIVE", footer)
        self.assertNotIn("\033[", strip_terminal_controls(footer))

    def test_follower_prints_events_footer_and_plain_daily_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = WorkflowDatabase(root / "lab.sqlite3")
            database.initialize()
            database.record_event(
                "supervisor", "worker", "synthesis_started",
                {"stage": "synthesizing", "requested": 25},
                occurred_at="2026-08-27T00:00:00Z",
            )
            output = io.StringIO()
            now = datetime(2026, 8, 27, 0, 0, 55, tzinfo=timezone.utc)
            config = ActivityLogConfig(
                repo=root, log_dir="logs/{date}_log",
            )
            follower = ActivityFollower(
                database, output=output, config=config,
                started_at="2026-08-27T00:00:00Z",
                color=False, links=False, clock=lambda: now,
                sleep=lambda _seconds: None,
            )
            follower.run(max_iterations=1)
            text = output.getvalue()
            self.assertIn("SYNTHESIS: Synthesising 25 new tests", text)
            self.assertIn("└─ SYNTHESIS", text)
            self.assertIn("(+55 sec)", text)
            log_path = root / "logs/2026-08-27_log"
            self.assertTrue(log_path.is_file())
            log = log_path.read_text()
            self.assertIn("SYNTHESIS: Synthesising 25 new tests", log)
            self.assertNotIn("\033[", log)

    def test_database_event_cursor_is_bounded_and_ordered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database = WorkflowDatabase(Path(tmp) / "lab.sqlite3")
            database.initialize()
            first = database.record_event("supervisor", "worker", "attention", {"detail": "one"})
            second = database.record_event("supervisor", "worker", "attention", {"detail": "two"})
            self.assertEqual(
                [row["id"] for row in database.events_after(first["id"])],
                [second["id"]],
            )
            self.assertEqual(database.latest_event_id(), second["id"])


if __name__ == "__main__":
    unittest.main()
