"""HPO route planning, safe defaults, and operator guidance.

The default policy is deliberately conservative: one BTC-USDT 1h route with
three disjoint historical periods.  It is a bootstrap policy for a local
research loop, not a claim that Jesse has every candle; operators can replace
it with a verified route file at any time.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .database import WorkflowDatabase


REQUIRED_SPLITS = ("hpo", "oos", "rolling")

DEFAULT_ROUTE = {
    "exchange": "Binance Perpetual Futures",
    "symbol": "BTC-USDT",
    "timeframe": "1h",
}
DEFAULT_ROUTE_PERIODS = {
    "hpo": ("2024-01-01", "2025-01-01"),
    "rolling": ("2025-01-01", "2026-01-01"),
    "oos": ("2026-01-01", "2026-04-01"),
}


def default_hpo_routes() -> dict[str, list[dict[str, str]]]:
    """Return fresh, disjoint bootstrap routes for a scheduled HPO study."""
    return {
        split: [{
            **DEFAULT_ROUTE,
            "start_date": start,
            "finish_date": finish,
        }]
        for split, (start, finish) in DEFAULT_ROUTE_PERIODS.items()
    }


@dataclass(frozen=True)
class HpoRoutePlan:
    """Serializable, read-only route readiness projection."""

    study_id: str
    strategy: str
    lifecycle_state: str
    splits: dict[str, dict[str, Any]]
    known_routes: tuple[dict[str, str], ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "study_id": self.study_id,
            "strategy": self.strategy,
            "lifecycle_state": self.lifecycle_state,
            "splits": self.splits,
            "known_routes": list(self.known_routes),
            "warnings": list(self.warnings),
            "required_file_shape": {split: [] for split in REQUIRED_SPLITS},
            "operator_command": (
                "ats-lab configure-hpo-validation-routes "
                f"{self.study_id} --file validation-routes.json"
            ),
        }


class HpoRoutePlanner:
    """Build an HPO route plan without changing workflow state."""

    def __init__(self, database: WorkflowDatabase):
        self.database = database

    def build(self, study_id: str) -> HpoRoutePlan:
        detail = self.database.hpo_study_detail(study_id)
        if detail is None:
            raise KeyError(f"unknown HPO study: {study_id}")
        experiment_id = detail.get("hpo_experiment_id")
        experiment = self.database.rows(
            "SELECT specification_json FROM experiments WHERE id=?",
            (experiment_id,),
        )
        specification = _json_object(
            experiment[0]["specification_json"] if experiment else None,
        )
        hpo_routes = _route_list(specification.get("routes"))
        validation_routes = specification.get("validation_routes")
        validation_routes = (
            validation_routes if isinstance(validation_routes, dict) else {}
        )
        configured: dict[str, list[dict[str, str]]] = {
            "hpo": hpo_routes,
            "oos": _route_list(validation_routes.get("oos")),
            "rolling": _route_list(validation_routes.get("rolling")),
        }
        splits = {
            split: {
                "role": {
                    "hpo": "optimizer training",
                    "oos": "unseen holdout",
                    "rolling": "unseen rolling validation",
                }[split],
                "required": True,
                "configured_routes": len(configured[split]),
                "ready": bool(configured[split]),
            }
            for split in REQUIRED_SPLITS
        }
        warnings = []
        missing = [split for split in REQUIRED_SPLITS if not configured[split]]
        if missing:
            warnings.append("missing route splits: " + ", ".join(missing))
        if hpo_routes and (validation_routes.get("oos") or validation_routes.get("rolling")):
            warnings.extend(_partition_warnings(configured))
        return HpoRoutePlan(
            study_id=study_id,
            strategy=str(detail.get("strategy") or ""),
            lifecycle_state=str(detail.get("lifecycle_state") or ""),
            splits=splits,
            known_routes=tuple(self._known_routes(str(detail.get("strategy") or ""))),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def default_payload(self) -> dict[str, list[dict[str, str]]]:
        """Expose the explicit bootstrap policy without database mutation."""
        return default_hpo_routes()

    def _known_routes(self, strategy: str) -> list[dict[str, str]]:
        """Return route shapes seen for this strategy, never recommendations."""
        rows = self.database.rows(
            """SELECT DISTINCT
                      json_extract(j.value,'$.route.exchange') AS exchange,
                      json_extract(j.value,'$.route.symbol') AS symbol,
                      json_extract(j.value,'$.route.timeframe') AS timeframe,
                      json_extract(j.value,'$.route.start_date') AS start_date,
                      json_extract(j.value,'$.route.finish_date') AS finish_date
                 FROM runs r
                 JOIN experiments e ON e.id=r.experiment_id
                 JOIN strategies s ON s.id=e.strategy_id
                 JOIN json_each(r.metrics_json,'$.route_runs') j
                WHERE s.name=?
                  AND json_extract(j.value,'$.route.exchange') IS NOT NULL
                  AND json_extract(j.value,'$.route.symbol') IS NOT NULL
                  AND json_extract(j.value,'$.route.timeframe') IS NOT NULL
                  AND json_extract(j.value,'$.route.start_date') IS NOT NULL
                  AND json_extract(j.value,'$.route.finish_date') IS NOT NULL
                ORDER BY finish_date DESC,start_date DESC
                LIMIT 25""",
            (strategy,),
        )
        return [
            {
                key: str(row[key])
                for key in ("exchange", "symbol", "timeframe", "start_date", "finish_date")
            }
            for row in rows
        ]


def render_hpo_route_plan(plan: HpoRoutePlan) -> str:
    """Render the small operator-facing route checklist."""
    payload = plan.to_dict()
    lines = [
        f"HPO ROUTES  {plan.study_id}  strategy={plan.strategy} "
        f"state={plan.lifecycle_state}",
        "split    role                         routes  readiness",
        "-------  ---------------------------  ------  ---------",
    ]
    for split in REQUIRED_SPLITS:
        item = payload["splits"][split]
        lines.append(
            f"{split:<7}  {item['role']:<27}  {item['configured_routes']:>6}  "
            f"{'ready' if item['ready'] else 'missing'}"
        )
    if payload["known_routes"]:
        lines.append("\nKNOWN ROUTE SHAPES (observed; not recommendations)")
        for route in payload["known_routes"][:8]:
            lines.append(
                "  " + " ".join(
                    route[key] for key in (
                        "exchange", "symbol", "timeframe",
                        "start_date", "finish_date",
                    )
                )
            )
    for warning in payload["warnings"]:
        lines.append(f"\nWARNING  {warning}")
    lines.append("\nDEFAULTS  ats-lab hpo-defaults --apply")
    lines.append("FILE SHAPE  {\"hpo\": [...], \"oos\": [...], \"rolling\": [...]}")
    lines.append(f"NEXT      {payload['operator_command']}")
    return "\n".join(lines)


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        decoded = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _route_list(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _partition_warnings(routes: dict[str, list[dict[str, str]]]) -> list[str]:
    """Detect train/validation date overlap for matching market routes."""
    warnings: list[str] = []
    training = routes["hpo"]
    for validation_split in ("oos", "rolling"):
        for train in training:
            for validation in routes[validation_split]:
                if _same_market(train, validation) and _overlap(train, validation):
                    warnings.append(
                        f"{validation_split} overlaps hpo training for "
                        f"{train.get('symbol')} {train.get('timeframe')}"
                    )
    return warnings


def _same_market(left: dict[str, str], right: dict[str, str]) -> bool:
    return tuple(left.get(key) for key in ("exchange", "symbol", "timeframe")) == tuple(
        right.get(key) for key in ("exchange", "symbol", "timeframe")
    )


def _overlap(left: dict[str, str], right: dict[str, str]) -> bool:
    try:
        from datetime import date

        left_start = date.fromisoformat(str(left["start_date"]))
        left_finish = date.fromisoformat(str(left["finish_date"]))
        right_start = date.fromisoformat(str(right["start_date"]))
        right_finish = date.fromisoformat(str(right["finish_date"]))
    except (KeyError, TypeError, ValueError):
        return False
    return left_start < right_finish and right_start < left_finish
