"""Small helpers shared across database seam modules."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .evidence import NormalizedEvidence
from .models import RouteSpec


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("evidence JSON must be an object")
    return decoded


_EVIDENCE_COLUMNS = tuple(NormalizedEvidence.__dataclass_fields__)
_EVIDENCE_FILTERS = frozenset(_EVIDENCE_COLUMNS)


def _route_payload(
    route: RouteSpec | dict[str, Any],
) -> dict[str, Any]:
    return dict(route) if isinstance(route, dict) else asdict(route)
