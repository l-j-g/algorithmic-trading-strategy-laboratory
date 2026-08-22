"""Human-facing formatting helpers shared by operator surfaces.

Stored timestamps stay canonical UTC ISO-8601 in SQLite and API payloads.
Only presentation layers call this module, so every operator surface shows
the same local-time, minute-precision rendering.
"""
from __future__ import annotations

from datetime import datetime, timezone


def human_time(value: object, *, default: str = "—") -> str:
    """Render a stored timestamp as local wall-clock time at minute precision.

    Naive values are treated as UTC, matching the canonical storage format.
    Unparseable input is returned unchanged so diagnostics never lose data.
    """
    if value in (None, ""):
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
