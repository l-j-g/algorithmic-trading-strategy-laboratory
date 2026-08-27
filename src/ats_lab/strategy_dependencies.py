"""Trusted auxiliary-candle dependencies for Jesse strategies.

Jesse keeps trading routes and non-trading data routes separate.  The strategy
source is private to the Jesse workspace, so ATS keeps only the reviewed,
machine-readable dependency contract here.  Explicit request routes are still
accepted; this module adds the trusted routes and deduplicates both metadata
levels before execution.
"""
from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .models import DataRouteSpec


PRIMARY_ROUTE_VALUE = "$primary"


def _manifest_path() -> Path:
    return Path(__file__).with_name("data") / "strategy-data-routes.json"


@lru_cache(maxsize=1)
def trusted_data_route_manifest() -> dict[str, tuple[dict[str, str], ...]]:
    """Load and validate the bundled strategy dependency manifest."""
    payload = json.loads(_manifest_path().read_text())
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("strategy data-route manifest schema_version must be 1")
    strategies = payload.get("strategies")
    if not isinstance(strategies, dict):
        raise ValueError("strategy data-route manifest strategies must be an object")
    result: dict[str, tuple[dict[str, str], ...]] = {}
    for strategy_name, routes in strategies.items():
        if not isinstance(strategy_name, str) or not strategy_name.strip():
            raise ValueError("strategy data-route manifest names must be non-empty text")
        if not isinstance(routes, list):
            raise ValueError(
                f"strategy data-route manifest entry must be an array: {strategy_name}"
            )
        normalized: list[dict[str, str]] = []
        for index, route in enumerate(routes):
            if not isinstance(route, dict):
                raise ValueError(
                    f"strategy data-route manifest route must be an object: "
                    f"{strategy_name}[{index}]"
                )
            if set(route) != {"exchange", "symbol", "timeframe"}:
                raise ValueError(
                    f"strategy data-route manifest route fields invalid: "
                    f"{strategy_name}[{index}]"
                )
            if any(
                not isinstance(value, str) or not value.strip()
                for value in route.values()
            ):
                raise ValueError(
                    f"strategy data-route manifest route values invalid: "
                    f"{strategy_name}[{index}]"
                )
            normalized.append(dict(route))
        result[strategy_name] = tuple(normalized)
    return result


def _primary_values(
    primary_routes: Sequence[Mapping[str, Any]] | None,
    field: str,
) -> tuple[str, ...]:
    values: list[str] = []
    for route in primary_routes or ():
        value = route.get(field)
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return tuple(values)


def required_data_routes(
    strategy_name: str,
    primary_routes: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[DataRouteSpec, ...]:
    """Resolve the reviewed auxiliary routes for one strategy.

    ``$primary`` in the manifest means the corresponding value from the
    trading route.  This supports strategies that request the same symbol on
    a higher timeframe without confusing it with a second trading route.
    """
    routes = trusted_data_route_manifest().get(strategy_name, ())
    primary_exchanges = _primary_values(primary_routes, "exchange")
    primary_timeframes = _primary_values(primary_routes, "timeframe")
    resolved: list[DataRouteSpec] = []
    for route in routes:
        exchanges = primary_exchanges if route["exchange"] == PRIMARY_ROUTE_VALUE else (route["exchange"],)
        timeframes = primary_timeframes if route["timeframe"] == PRIMARY_ROUTE_VALUE else (route["timeframe"],)
        for exchange in exchanges:
            for timeframe in timeframes:
                candidate = DataRouteSpec(
                    exchange=exchange, symbol=route["symbol"], timeframe=timeframe,
                )
                if candidate not in resolved:
                    resolved.append(candidate)
    return tuple(resolved)


def _coerce_data_route(value: DataRouteSpec | Mapping[str, Any], label: str) -> DataRouteSpec:
    if isinstance(value, DataRouteSpec):
        return value
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must contain data-route objects")
    try:
        return DataRouteSpec(
            exchange=value["exchange"],
            symbol=value["symbol"],
            timeframe=value["timeframe"],
        )
    except (KeyError, TypeError) as error:
        raise ValueError(f"{label} contains an invalid data route") from error


def merge_data_routes(
    strategy_name: str,
    primary_routes: Sequence[Mapping[str, Any]] | None,
    *sources: Sequence[DataRouteSpec | Mapping[str, Any]] | None,
) -> tuple[DataRouteSpec, ...]:
    """Union explicit route metadata with trusted strategy dependencies.

    Experiment-level and work-item-level lists are both included.  First
    occurrence wins, preserving deterministic request fingerprints.
    """
    merged: list[DataRouteSpec] = []
    for source_index, source in enumerate(sources):
        if source is None:
            continue
        if not isinstance(source, (list, tuple)):
            raise ValueError(f"data_routes source {source_index} must be an array")
        for route_index, value in enumerate(source):
            route = _coerce_data_route(
                value, f"data_routes source {source_index}[{route_index}]",
            )
            if route not in merged:
                merged.append(route)
    for route in required_data_routes(strategy_name, primary_routes):
        if route not in merged:
            merged.append(route)
    return tuple(merged)


def data_route_dicts(routes: Sequence[DataRouteSpec]) -> list[dict[str, str]]:
    """Return JSON-safe route dictionaries for ATS and Jesse payloads."""
    return [asdict(route) for route in routes]
