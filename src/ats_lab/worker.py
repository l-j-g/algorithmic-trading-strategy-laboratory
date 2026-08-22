"""Continuous queue worker with an agent-neutral dispatch boundary."""
from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .contracts import evaluation_from_payload
from .database import WorkflowDatabase
from .batch_synthesis import apply_batch, build_batch_context
from .models import RouteSpec, RunResult, RunStatus, WorkState
from .resources import ResourcePolicy


@dataclass(frozen=True)
class DispatchResult:
    outcome: str
    blocker_code: str | None = None
    detail: str | None = None
    retry_after: str | None = None
    payload: dict[str, Any] | None = None


class Dispatcher(Protocol):
    def dispatch(self, request: dict[str, Any]) -> DispatchResult: ...


class CommandDispatcher:
    """Send one request as JSON on stdin; read one JSON result from stdout."""

    def __init__(self, command: str):
        if not command.strip():
            raise ValueError("dispatch command must not be empty")
        self.command = shlex.split(command)

    def dispatch(self, request: dict[str, Any]) -> DispatchResult:
        print(json.dumps({
            "status": "dispatching",
            "task_type": request.get("task_type", "execute"),
            "work_item_id": request.get("work_item_id"),
        }, sort_keys=True), file=sys.stderr, flush=True)
        completed = subprocess.run(
            self.command,
            input=json.dumps(request),
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or f"dispatcher exited {completed.returncode}"
            return DispatchResult(outcome="retry", blocker_code="dispatcher_failed", detail=detail)
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            return DispatchResult(outcome="retry", blocker_code="invalid_dispatch_result", detail=str(error))
        if not isinstance(payload, dict):
            return DispatchResult(outcome="retry", blocker_code="invalid_dispatch_result", detail="result must be an object")
        return DispatchResult(
            outcome=str(payload.get("outcome", "")),
            blocker_code=payload.get("blocker_code"),
            detail=payload.get("detail"),
            retry_after=payload.get("retry_after"),
            payload=payload,
        )


class Worker:
    def __init__(
        self,
        database: WorkflowDatabase,
        dispatcher: Dispatcher,
        worker_id: str,
        *,
        retry_delay_seconds: float = 60,
        max_attempts: int = 5,
        synthesize_when_idle: bool = False,
        resource_policy: ResourcePolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.database = database
        self.dispatcher = dispatcher
        self.worker_id = worker_id
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts
        self.synthesize_when_idle = synthesize_when_idle
        self.resource_policy = resource_policy or ResourcePolicy()
        self.sleep = sleep
        self.started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._recovered_startup_claims = False

    def run_once(self) -> dict[str, Any]:
        if not self._recovered_startup_claims:
            self.database.recover_abandoned_claims(self.worker_id, self.started_at)
            self._recovered_startup_claims = True
        self.database.refresh_synthesis_cohorts()
        promoted = self.database.promote_due_retries()
        promoted += self.database.promote_scheduled_runnable(self.resource_policy.active_ready_limit)
        if self.synthesize_when_idle:
            cohort = self.database.reserve_synthesis_cohort(
                worker_id=self.worker_id,
                requested_count=self.resource_policy.synthesis_generate_limit,
                low_watermark=self.resource_policy.synthesis_low_watermark,
                lease_seconds=self.resource_policy.synthesis_lease_seconds,
                retry_cooldown_seconds=self.resource_policy.synthesis_retry_cooldown_seconds,
            )
            if cohort is not None:
                return self._synthesize_replenishment(cohort)
        claimed = self.database.claim_next(self.worker_id)
        if claimed is None:
            return {"status": "idle", "promoted_retries": promoted}

        request = self.database.execution_request(claimed["id"])
        request["resource_policy"] = self.resource_policy.to_dict()
        try:
            result = self.dispatcher.dispatch(request)
        except Exception as error:  # keep queue recoverable across adapter failures
            result = DispatchResult(outcome="retry", blocker_code="dispatcher_exception", detail=str(error))

        experiment = request.get("experiment", {})
        operation = request.get("work_item", {}).get("operation") or experiment.get("operation")
        if not operation:
            operation = {
                "baseline": "backtest",
                "multi_window": "backtest",
                "cost_sensitivity": "backtest",
                "out_of_sample": "backtest",
                "harness_check": "backtest",
                "significance": "significance",
                "monte_carlo": "monte_carlo",
                "hpo": "hpo",
            }.get(experiment.get("experiment_type"))
        research_evidence: dict[str, Any] | None = None
        if result.outcome == "finished" and operation in {"significance", "backtest", "hpo", "monte_carlo"}:
            try:
                research_evidence = self._persist_research_evidence(request, result)
            except (KeyError, TypeError, ValueError) as error:
                result = DispatchResult(
                    outcome="retry", blocker_code="invalid_run_evidence",
                    detail=str(error), payload=result.payload,
                )

        if result.outcome == "finished":
            item = self.database.transition_work_item(
                claimed["id"], WorkState.FINISHED, allowed_from=(WorkState.RUNNING,)
            )
            if operation == "significance" and research_evidence is not None:
                self.database.reconcile_significance_gate(
                    claimed["id"], research_evidence["p_value"],
                    self.resource_policy.active_ready_limit,
                    fdr_level=self.resource_policy.significance_fdr_level,
                )
        elif result.outcome == "blocked":
            item = self.database.transition_work_item(
                claimed["id"], WorkState.BLOCKED, allowed_from=(WorkState.RUNNING,),
                blocker_code=result.blocker_code or "dispatcher_blocked",
                blocker_detail=result.detail or "dispatcher blocked work",
            )
        elif result.outcome == "retry":
            next_attempt = claimed["attempts"] + 1
            if next_attempt >= self.max_attempts:
                item = self.database.transition_work_item(
                    claimed["id"], WorkState.BLOCKED, allowed_from=(WorkState.RUNNING,),
                    blocker_code="retry_limit_reached",
                    blocker_detail=f"{result.blocker_code or 'retry'} after {next_attempt} attempts: {result.detail or ''}".strip(),
                )
            else:
                delay = self.retry_delay_seconds * (2 ** max(0, next_attempt - 1))
                retry_after = result.retry_after or (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat().replace("+00:00", "Z")
                item = self.database.transition_work_item(
                    claimed["id"], WorkState.WAITING_RETRY, allowed_from=(WorkState.RUNNING,),
                    blocker_code=result.blocker_code, blocker_detail=result.detail, retry_after=retry_after,
                )
        else:
            item = self.database.transition_work_item(
                claimed["id"], WorkState.BLOCKED, allowed_from=(WorkState.RUNNING,),
                blocker_code="invalid_dispatch_outcome",
                blocker_detail=f"unsupported outcome: {result.outcome!r}",
            )
        return {"status": item["state"], "work_item_id": item["id"], "dispatch": result.payload}

    def _synthesize_replenishment(self, cohort: dict[str, Any]) -> dict[str, Any]:
        request = {
            "schema_version": 1,
            "task_type": "synthesize_batch",
            "instruction": (
                f"Plan one cohort of exactly {self.resource_policy.synthesis_generate_limit} "
                "research chains. Resolve revise outcomes first, then fill remaining slots "
                "with diverse new hypotheses. Use eligible improvements first, at least "
                f"{self.resource_policy.synthesis_min_new_concepts} new concepts, and at most "
                f"{self.resource_policy.synthesis_max_improvements} improvements. Use prior "
                "metrics and failure notes for controlled improvements."
            ),
            "cohort": cohort,
            "context": build_batch_context(self.database, policy=self.resource_policy),
        }
        try:
            result = self.dispatcher.dispatch(request)
        except Exception as error:
            self.database.fail_synthesis_cohort(cohort["id"], str(error))
            return {"status": "synthesis_failed", "detail": str(error)}
        payload = result.payload or {}
        synthesis_payloads = payload.get("evidence", {}).get("synthesis_requests") if isinstance(payload.get("evidence"), dict) else None
        if result.outcome != "finished" or not isinstance(synthesis_payloads, list):
            detail = result.detail or "dispatcher must return evidence.synthesis_requests array"
            self.database.fail_synthesis_cohort(cohort["id"], detail)
            return {
                "status": "synthesis_failed",
                "blocker_code": result.blocker_code or "invalid_synthesis_result",
                "detail": detail,
            }
        try:
            synthesized = apply_batch(
                self.database, synthesis_payloads, policy=self.resource_policy, cohort_id=cohort["id"],
            )
            if synthesized["rejected"] or len(synthesized["generated"]) != cohort["requested_count"]:
                raise ValueError(f"incomplete synthesis cohort: {synthesized}")
            chains = []
            for generated in synthesized["generated"]:
                work_item_ids = [
                    item_id for item_id in (
                        generated.get("significance_job"), generated["baseline_job"],
                    ) if item_id
                ]
                chains.append({
                    "slot": generated["cohort_slot"], "lane": generated["lane"],
                    "source_experiment_id": generated["source_experiment_id"],
                    "work_item_ids": work_item_ids,
                })
            self.database.activate_synthesis_cohort(cohort["id"], chains)
        except (KeyError, TypeError, ValueError) as error:
            self.database.fail_synthesis_cohort(cohort["id"], str(error))
            return {
                "status": "synthesis_failed", "blocker_code": "invalid_synthesis_batch",
                "detail": str(error),
            }
        return {"status": "synthesized", "cohort_id": cohort["id"], "synthesis": synthesized}

    def _persist_research_evidence(self, request: dict[str, Any], result: DispatchResult) -> dict[str, Any]:
        evidence = (result.payload or {}).get("evidence")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("run"), dict):
            raise ValueError("finished research work requires evidence.run")
        if not isinstance(evidence.get("evaluation"), dict):
            raise ValueError("finished research work requires evidence.evaluation")
        run = evidence["run"]
        session_id = str(run.get("session_id") or "")
        if not session_id:
            raise ValueError("evidence.run.session_id is required")
        metrics = run.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("evidence.run.metrics must be an object")
        route_payload = run.get("route")
        route = None
        if isinstance(route_payload, dict):
            route = RouteSpec(**{
                key: route_payload[key]
                for key in ("exchange", "symbol", "timeframe", "start_date", "finish_date")
                if key in route_payload
            })
        evaluation_payload = dict(evidence["evaluation"])
        evaluation_payload.setdefault("experiment_id", request["experiment_id"])
        if evaluation_payload["experiment_id"] != request["experiment_id"]:
            raise ValueError("evidence.evaluation experiment_id must match request")
        missing = [
            name for name in ("verdict", "summary", "metrics_summary", "next_step")
            if not evaluation_payload.get(name)
        ]
        if missing:
            raise ValueError("evidence.evaluation missing fields: " + ", ".join(missing))
        evaluation = evaluation_from_payload(evaluation_payload)
        operation = request.get("work_item", {}).get("operation")
        p_value = None
        if operation == "significance":
            if metrics.get("p_value") is None:
                raise ValueError("significance metrics require p_value")
            p_value = float(metrics["p_value"])
            expected = "pass" if p_value < 0.05 else ("inconclusive" if p_value <= 0.10 else "reject")
            if evaluation.verdict.value != expected:
                raise ValueError(
                    f"significance evaluation verdict must be {expected} for p_value {p_value}"
                )
        run_result = RunResult(
            id=str(run.get("id") or f"{request['work_item_id']}:{session_id}"),
            experiment_id=request["experiment_id"], work_item_id=request["work_item_id"],
            session_id=session_id, status=RunStatus(str(run.get("status", "finished"))),
            route=route, dashboard_url=run.get("dashboard_url"), metrics=metrics,
            error=run.get("error"), started_at=run.get("started_at"), finished_at=run.get("finished_at"),
        )
        self.database.add_run_and_evaluation(run_result, evaluation)
        return {"p_value": p_value}

    def run(
        self, *, continuous: bool, idle_sleep: float, max_items: int | None = None,
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        processed = 0
        while True:
            result = self.run_once()
            if on_result is not None:
                on_result(result)
            if not continuous or max_items is not None:
                results.append(result)
            if result["status"] not in {"idle", "synthesis_failed"}:
                processed += 1
            if max_items is not None and processed >= max_items:
                return results
            if not continuous:
                return results
            if result["status"] in {"idle", "synthesis_failed"}:
                self.sleep(idle_sleep)
