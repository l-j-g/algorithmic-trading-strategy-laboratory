"""Small helpers shared across database seam modules."""

from __future__ import annotations

import json
from typing import Any


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("evidence JSON must be an object")
    return decoded
