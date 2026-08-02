from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ats_lab.retry_schedule import resolve_retry_after


class RetryScheduleTests(unittest.TestCase):
    def test_relative_seconds_become_absolute_utc_timestamp(self) -> None:
        now = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)

        result = resolve_retry_after("30", default_seconds=60, now=now)

        self.assertEqual(result, "2026-08-02T10:00:30Z")

    def test_absolute_timestamp_is_normalized(self) -> None:
        result = resolve_retry_after(
            "2026-08-02T20:00:00+10:00", default_seconds=60,
        )

        self.assertEqual(result, "2026-08-02T10:00:00Z")

    def test_invalid_or_negative_schedule_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid retry schedule"):
            resolve_retry_after("later", default_seconds=60)
        with self.assertRaisesRegex(ValueError, "cannot be negative"):
            resolve_retry_after("-1", default_seconds=60)


if __name__ == "__main__":
    unittest.main()
