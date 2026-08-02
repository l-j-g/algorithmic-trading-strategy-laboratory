"""Normalize absolute and HTTP-style relative retry schedules."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime


def utc_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_retry_after(
    value: object | None,
    *,
    default_seconds: float,
    now: datetime | None = None,
) -> str:
    """Return one UTC timestamp from ISO, HTTP date, or delay seconds."""
    current = now or datetime.now(timezone.utc)
    if value is None or str(value).strip() == "":
        return utc_timestamp(current + timedelta(seconds=default_seconds))
    text = str(value).strip()
    try:
        seconds = float(text)
    except ValueError:
        seconds = None
    if seconds is not None:
        if seconds < 0:
            raise ValueError("retry delay cannot be negative")
        return utc_timestamp(current + timedelta(seconds=seconds))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid retry schedule: {text}") from error
    if parsed.tzinfo is None:
        raise ValueError("retry timestamp must include a timezone")
    return utc_timestamp(parsed)
