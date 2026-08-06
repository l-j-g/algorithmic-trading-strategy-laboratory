"""Bounded Jesse strategy contract checks used before expensive execution.

The laboratory cannot inspect private strategy source.  Preparation therefore
returns a small, explicit contract receipt.  This module validates that receipt
and catches the common mechanical failures that otherwise waste a backtest:
non-positive sizing, scalar exit targets, indicator API mismatches, and callback
signature mismatches.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


REQUIRED_READINESS_CHECKS = (
    "positive_quantity",
    "exit_shape",
    "indicator_api",
    "callback_api",
)


@dataclass(frozen=True)
class ContractIssue:
    """One bounded, machine-readable strategy contract failure."""

    code: str
    detail: str


@dataclass(frozen=True)
class ReadinessValidation:
    """Normalized preparation receipt status."""

    status: str
    detail: str
    malformed: bool = False


class StrategyContractValidator:
    """Validate request metadata and model-reported Jesse contract checks."""

    def validate_request(self, request: Mapping[str, Any]) -> tuple[ContractIssue, ...]:
        """Catch explicit metadata defects without reading strategy source."""
        experiment = request.get("experiment")
        if not isinstance(experiment, Mapping):
            return (ContractIssue("missing_experiment", "experiment metadata is required"),)
        issues: list[ContractIssue] = []

        sizing = experiment.get("sizing_model")
        if sizing is not None and not isinstance(sizing, str):
            issues.append(ContractIssue("invalid_sizing_model", "sizing_model must be text"))
        elif isinstance(sizing, str):
            normalized = " ".join(sizing.casefold().split())
            if "starting balance" in normalized:
                issues.append(ContractIssue(
                    "starting_balance_sizing",
                    "risk sizing must use available_margin, not starting balance",
                ))
            if "risk_to_qty" in normalized and (
                "available_margin" not in normalized or "95%" not in normalized
            ):
                issues.append(ContractIssue(
                    "uncapped_risk_sizing",
                    "risk_to_qty sizing must cap entry notional at 95% available_margin",
                ))

        entry_rule = experiment.get("entry_rule")
        if isinstance(entry_rule, Mapping):
            for name in ("stop_loss", "take_profit"):
                if name not in entry_rule:
                    continue
                value = entry_rule[name]
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    issues.append(ContractIssue(
                        "scalar_exit_target",
                        f"{name} must be a Jesse quantity/price sequence, not a scalar",
                    ))
                elif isinstance(value, str):
                    issues.append(ContractIssue(
                        "scalar_exit_target",
                        f"{name} must be a Jesse quantity/price sequence, not text",
                    ))
        return tuple(issues)

    def validate_readiness(self, entry: Mapping[str, Any]) -> ReadinessValidation:
        """Normalize one preparation receipt; malformed receipts remain retryable."""
        status = entry.get("status")
        if status not in {"ready", "missing", "invalid"}:
            return ReadinessValidation(
                "invalid", "strategy readiness status must be ready, missing, or invalid", True,
            )
        detail = " ".join(str(entry.get("detail") or "").split())[:1000]
        if status != "ready":
            if not detail:
                return ReadinessValidation(
                    str(status), "non-ready strategy requires detail", True,
                )
            return ReadinessValidation(str(status), detail)

        checks = entry.get("contract_checks")
        if not isinstance(checks, list):
            return ReadinessValidation(
                "invalid",
                "ready strategy requires contract_checks for sizing, exits, indicators, and callbacks",
                True,
            )
        by_code: dict[str, Mapping[str, Any]] = {}
        for check in checks:
            if not isinstance(check, Mapping):
                return ReadinessValidation("invalid", "contract_checks entries must be objects", True)
            code = check.get("code")
            state = check.get("status")
            if not isinstance(code, str) or not code.strip():
                return ReadinessValidation("invalid", "contract_checks code must be non-empty text", True)
            if state not in {"pass", "fail"}:
                return ReadinessValidation(
                    "invalid", f"contract check {code} status must be pass or fail", True,
                )
            if code in by_code:
                return ReadinessValidation("invalid", f"duplicate contract check {code}", True)
            by_code[code] = check
        missing = [code for code in REQUIRED_READINESS_CHECKS if code not in by_code]
        if missing:
            return ReadinessValidation(
                "invalid", "missing required contract checks: " + ", ".join(missing), True,
            )
        failed: list[str] = []
        for code in REQUIRED_READINESS_CHECKS:
            check = by_code[code]
            if check.get("status") == "fail":
                check_detail = " ".join(str(check.get("detail") or "contract check failed").split())
                failed.append(f"{code}: {check_detail[:240]}")
        if failed:
            return ReadinessValidation("invalid", "; ".join(failed)[:1000])
        return ReadinessValidation("ready", detail or "Jesse strategy contract checks passed")
