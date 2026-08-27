"""Batch-first execution, isolated analysis, and cohort replenishment."""
from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .analysis_input import ExecutionAnalysisInputBuilder
from .batch_synthesis import apply_batch, build_batch_context
from .contracts import evaluation_from_payload
from .database import WorkflowDatabase
from .evidence import MACHINE_COST_STRESS_SUFFIX, NormalizedEvidence
from .execution_disposition import (
    INFRASTRUCTURE_FAILURE_CODES,
    ExecutionDispositionPolicy,
    ExecutionFailureRecorder,
    ExecutionRoute,
    TerminalFailureRecovery,
)
from .gates import (
    GateDecision,
    evaluate_gates,
    evaluate_hpo_candidate,
    evaluate_promotion,
)
from .hpo_routes import default_hpo_routes
from .models import (
    Evaluation,
    ExperimentSpec,
    ExperimentType,
    RouteSpec,
    RunResult,
    RunStatus,
    Verdict,
    WorkItem,
    WorkState,
)
from .resources import ResourcePolicy
from .runtime_heartbeat import RuntimeHeartbeat
from .research_memory import (
    ResearchMemoryAdapter,
    compact_advisory_memory,
    sync_memory_outbox,
)
from .retry_schedule import resolve_retry_after
from .status import operator_status
from .worker import DispatchResult, Dispatcher


INFRASTRUCTURE_BLOCKERS = INFRASTRUCTURE_FAILURE_CODES

# Analyzer memory is a small advisory hint, not a second evidence payload.
# Keep this below the synthesis memory budget so continuity cannot materially
# inflate analyzer turns.
ANALYZER_MEMORY_MAX_ITEMS = 3
ANALYZER_MEMORY_MAX_BYTES = 3_200
ANALYZER_MEMORY_MAX_TEXT_CHARS = 240


class BatchSupervisor:
    """Spend one agent turn executing a batch, then one turn judging the batch."""

    def __init__(
        self,
        database: WorkflowDatabase,
        dispatcher: Dispatcher,
        worker_id: str,
        *,
        resource_policy: ResourcePolicy | None = None,
        retry_delay_seconds: float = 60,
        max_attempts: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        preflight: Callable[[], dict[str, Any]] | None = None,
        memory_adapter: ResearchMemoryAdapter | None = None,
        disposition_policy: ExecutionDispositionPolicy | None = None,
        failure_recorder: ExecutionFailureRecorder | None = None,
        analysis_input_builder: ExecutionAnalysisInputBuilder | None = None,
        terminal_failure_recovery: TerminalFailureRecovery | None = None,
        heartbeat_interval_seconds: float = 30.0,
    ):
        self.database = database
        self.dispatcher = dispatcher
        self.worker_id = worker_id
        self.resource_policy = resource_policy or ResourcePolicy()
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.preflight = preflight
        self.memory_adapter = memory_adapter
        self.disposition_policy = (
            disposition_policy or ExecutionDispositionPolicy()
        )
        self.failure_recorder = failure_recorder or ExecutionFailureRecorder(
            database, worker_id,
        )
        self.analysis_input_builder = (
            analysis_input_builder or ExecutionAnalysisInputBuilder()
        )
        self.terminal_failure_recovery = (
            terminal_failure_recovery
            or TerminalFailureRecovery(database, self.failure_recorder)
        )
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._last_preflight_signature: str | None = None
        self._last_attention_signature: str | None = None

    def plan(self) -> dict[str, Any]:
        result = operator_status(self.database)
        result["control"] = self.database.control_status()
        result["supervisor"] = self.database.supervisor_runtime_status()
        result["policy"] = {
            "execution_batch_size": self.resource_policy.execution_batch_size,
            "analysis_cohort_min": self.resource_policy.analysis_cohort_min,
            "analysis_cohort_max": self.resource_policy.analysis_cohort_max,
            "analysis_parallelism": self.resource_policy.analysis_parallelism,
            "analyzer_timeout_seconds": self.resource_policy.analyzer_timeout_seconds,
            "synthesis_generate_limit": self.resource_policy.synthesis_generate_limit,
            "synthesis_low_watermark": self.resource_policy.synthesis_low_watermark,
        }
        return result

    def run_round(self) -> dict[str, Any]:
        self._runtime("checking")
        desired_state = self.database.control_status()["desired_state"]
        # Pause must gate pending analysis too.  Checking pending work first
        # lets a paused supervisor continue dispatching analyzer turns (and
        # therefore keep Hermes/Jesse traffic alive).
        if desired_state == "paused":
            self._runtime("paused")
            return {
                "status": "paused",
                "operator": operator_status(self.database),
            }

        # Stop must win over pending analysis so a broken analyzer provider
        # cannot prevent graceful supervisor shutdown indefinitely.
        if desired_state == "stop_requested":
            self._runtime("stopping")
            return {
                "status": "stop_requested",
                "operator": operator_status(self.database),
            }

        pending = self.database.pending_batch_evaluation(self.worker_id)
        ready_for_analysis = self._analysis_ready_rows(pending)
        if ready_for_analysis:
            return self._analyze_pending(
                ready_for_analysis, recovered=0, promoted=0,
            )

        if self.preflight is not None:
            infrastructure = self.preflight()
            safe_checks = [
                {
                    "name": item.get("name"),
                    "status": item.get("status"),
                }
                for item in infrastructure.get("checks", [])
                if isinstance(item, dict)
            ]
            preflight_signature = json.dumps(
                safe_checks, sort_keys=True, separators=(",", ":"),
            )
            if preflight_signature != self._last_preflight_signature:
                self._activity(
                    "preflight_completed",
                    {
                        "healthy": bool(infrastructure.get("healthy")),
                        "checks": safe_checks,
                    },
                )
                self._last_preflight_signature = preflight_signature
            if not infrastructure.get("healthy"):
                self._runtime("infrastructure_blocked", detail=infrastructure)
                attention = str(
                    infrastructure.get("detail")
                    or infrastructure.get("failed_check")
                    or "infrastructure requires attention"
                )
                if attention != self._last_attention_signature:
                    self._activity(
                        "attention",
                        {
                            "stage": "infrastructure_blocked",
                            "detail": attention,
                        },
                    )
                    self._last_attention_signature = attention
                return {
                    "status": "infrastructure_blocked",
                    "blocker_code": infrastructure.get("blocker_code"),
                    "failed_check": infrastructure.get("failed_check"),
                    "detail": infrastructure.get("detail"),
                }
            self._last_attention_signature = None

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=self.resource_policy.claim_timeout_seconds)
        ).isoformat().replace("+00:00", "Z")
        recovered = len(self.database.recover_stale_unexecuted_claims(
            cutoff, apply=True,
        )["recoverable"])
        if hasattr(self.database, "recover_abandoned_hpo_analysis"):
            recovered += len(
                self.database.recover_abandoned_hpo_analysis(cutoff)
            )
        recovery_batch = f"ANALYZE-FAILURES-{uuid.uuid4().hex[:12].upper()}"
        recovered_failures = self.terminal_failure_recovery.recover(
            batch_id=recovery_batch,
            limit=self.resource_policy.analysis_cohort_max,
        )
        if recovered_failures:
            pending_failures = [
                row for row in self.database.pending_batch_evaluation(
                    self.worker_id,
                )
                if row["batch_id"] == recovery_batch
            ]
            ready_failures = self._analysis_ready_rows(pending_failures)
            if ready_failures:
                return self._analyze_pending(
                    ready_failures, recovered=recovered, promoted=0,
                )
            return {
                "status": "awaiting_analysis_cohort",
                "batch_id": recovery_batch,
                "pending_items": len(pending_failures),
                "minimum_items": self.resource_policy.analysis_cohort_min,
                "recovered": recovered,
                "promoted": 0,
            }
        if hasattr(self.database, "reconcile_finished_hpo_work"):
            self.database.reconcile_finished_hpo_work()
        self._apply_default_hpo_routes()
        if hasattr(self.database, "mark_unroutable_hpo_requirements_pending"):
            self.database.mark_unroutable_hpo_requirements_pending()
        self.database.refresh_synthesis_cohorts()
        promoted = self.database.promote_due_retries()
        promoted += self.database.promote_scheduled_runnable(
            self.resource_policy.active_ready_limit
        )

        if hasattr(self.database, "claim_hpo_analysis"):
            hpo_job = self.database.claim_hpo_analysis(
                self.worker_id,
                cohort_id=f"HPO-COHORT-{uuid.uuid4().hex[:12].upper()}",
            )
            if hpo_job:
                return self._analyze_hpo_job(
                    hpo_job, recovered=recovered, promoted=promoted,
                )

        claimed = self.database.claim_batch(
            self.worker_id, self.resource_policy.execution_batch_size,
        )
        if claimed:
            return self._execute(claimed, recovered=recovered, promoted=promoted)

        cohort = self._reserve_cohort()
        if cohort:
            return self._synthesize(
                cohort, recovered=recovered, promoted=promoted,
            )
        return {
            "status": "idle", "recovered": recovered, "promoted": promoted,
            "operator": operator_status(self.database),
        }

    def _apply_default_hpo_routes(self) -> list[str]:
        """Bootstrap only untouched scheduled HPO studies.

        Explicit route files always win. Partial or already-running studies
        are excluded by the database guard. This keeps the continuous loop
        moving while preserving operator-owned route decisions.
        """
        if not hasattr(self.database, "hpo_studies_needing_default_routes"):
            return []
        applied: list[str] = []
        for study in self.database.hpo_studies_needing_default_routes():
            study_id = str(study["study_id"])
            self.database.configure_default_hpo_routes(
                study_id, default_hpo_routes(self.resource_policy),
            )
            applied.append(study_id)
        if applied:
            self._runtime(
                "route_defaults_applied",
                detail={"studies": applied},
            )
        return applied

    def _execute(
        self, claimed: list[dict], *, recovered: int, promoted: int,
    ) -> dict[str, Any]:
        batch_id = f"BATCH-{uuid.uuid4().hex[:12].upper()}"
        self._runtime(
            "executing", batch_id=batch_id,
            detail={"work_items": [item["id"] for item in claimed]},
        )
        for item in claimed:
            self._record_stage(
                item["id"], "queue_wait",
                duration_ms=self._timestamp_delta_ms(
                    item.get("created_at"), item.get("claimed_at"),
                ),
                state="finished", cohort_id=batch_id,
            )
        requests = []
        for item in claimed:
            request = self.database.execution_request(item["id"])
            study = (
                self.database.hpo_study_for_work_item(item["id"])
                if hasattr(self.database, "hpo_study_for_work_item")
                else None
            )
            if (
                study
                and study.get("lifecycle_state") == "hpo_scheduled"
                and hasattr(self.database, "start_hpo_study")
            ):
                study_id = study.get("study_id") or study.get("id")
                if study_id:
                    self.database.start_hpo_study(str(study_id))
            request["resource_policy"] = self.resource_policy.to_dict()
            requests.append(request)
        for request in requests:
            self._activity(
                "run_started",
                self._activity_request_payload(
                    request, operation=self._operation_for_request(request),
                    batch_id=batch_id,
                ) | {
                    "total": len(requests),
                    "completed": 0,
                    "usage": {},
                },
            )
        execution_started = time.perf_counter()
        dispatch = self._dispatch({
            "schema_version": 1,
            "task_type": "execute_batch",
            "batch_id": batch_id,
            "instruction": (
                "Execute every work item mechanically in this one bounded turn. "
                "Do not evaluate, revise, synthesize, or edit laboratory state."
            ),
            "requests": requests,
        })
        execution_ms = (time.perf_counter() - execution_started) * 1000
        for item in claimed:
            self._record_stage(
                item["id"], "execution", duration_ms=execution_ms,
                state=(
                    "finished" if dispatch.outcome == "finished" else "failed"
                ),
                cohort_id=batch_id,
            )
        payload = dispatch.payload or {}
        results = payload.get("results")
        if not isinstance(results, list):
            detail = dispatch.detail or "batch executor requires results array"
            for item in claimed:
                self._retry_or_block(item, dispatch.blocker_code or "batch_execution_failed", detail)
                request = self.database.execution_request(item["id"])
                self._activity(
                    "run_failed",
                    self._activity_request_payload(
                        request, operation=self._operation_for_request(request),
                        batch_id=batch_id,
                    ) | {
                        "total": len(claimed), "completed": 0,
                        "blocker_code": dispatch.blocker_code or "batch_execution_failed",
                        "detail": detail,
                        "usage": self._dispatch_usage(dispatch),
                    },
                )
            return {
                "status": "execution_failed", "batch_id": batch_id,
                "work_items": [item["id"] for item in claimed], "detail": detail,
                "recovered": recovered, "promoted": promoted,
            }
        by_id = {
            result.get("work_item_id"): result
            for result in results if isinstance(result, dict)
        }
        expected = {item["id"] for item in claimed}
        unknown = set(by_id) - expected

        awaiting: list[str] = []
        terminal: list[dict[str, str]] = []
        processed = 0
        for item in claimed:
            result = by_id.get(item["id"])
            if result is None:
                detail = (
                    f"batch result missing work item {item['id']}; "
                    f"received={sorted(key for key in by_id if key)}"
                )
                self._retry_or_block(
                    item, "batch_coverage_mismatch", detail,
                )
                terminal.append({
                    "work_item_id": item["id"], "state": "retry",
                })
                processed += 1
                request = self.database.execution_request(item["id"])
                self._activity(
                    "run_failed",
                    self._activity_request_payload(
                        request, operation=self._operation_for_request(request),
                        batch_id=batch_id,
                    ) | {
                        "total": len(claimed), "completed": processed,
                        "blocker_code": "batch_coverage_mismatch",
                        "detail": detail,
                        "usage": self._dispatch_usage(dispatch),
                    },
                )
                continue
            outcome = result.get("outcome")
            if outcome == "finished":
                try:
                    persistence_started = time.perf_counter()
                    execution_request = self.database.execution_request(item["id"])
                    self._persist_run(execution_request, result)
                    persistence_ms = (
                        time.perf_counter() - persistence_started
                    ) * 1000
                    self._record_stage(
                        item["id"], "persistence",
                        duration_ms=persistence_ms, state="finished",
                        cohort_id=batch_id,
                    )
                    hpo_study = (
                        self.database.hpo_study_for_work_item(item["id"])
                        if hasattr(self.database, "hpo_study_for_work_item")
                        else None
                    )
                    if hpo_study:
                        study_id = (
                            hpo_study.get("study_id")
                            or hpo_study.get("id")
                        )
                        if study_id:
                            # A scheduled HPO run must persist completed
                            # optimizer trials before analysis can claim it.
                            # Park an empty handoff for explicit import instead
                            # of burning analyzer retries on an empty payload.
                            self.database.complete_hpo_study(
                                str(study_id), require_trial_evidence=True,
                            )
                        self.database.add_evaluation(Evaluation(
                            experiment_id=item["experiment_id"],
                            verdict=Verdict.PASS,
                            summary=(
                                "HPO execution durable; separate HPO analysis "
                                "scheduled."
                            ),
                            metrics_summary=json.dumps(
                                [
                                    evidence.to_compact_dict()
                                    for evidence in
                                    self.database.normalized_evidence_for_experiment(
                                        item["experiment_id"]
                                    )
                                ],
                                separators=(",", ":"), sort_keys=True,
                            ),
                            next_step=(
                                "Analyze stable parameter regions and schedule "
                                "rolling multi-market validation."
                            ),
                            evaluator="ats-lab-hpo-execution",
                        ))
                        self.database.transition_work_item(
                            item["id"], WorkState.FINISHED,
                            allowed_from=(WorkState.RUNNING,),
                        )
                        terminal.append({
                            "work_item_id": item["id"],
                            "state": "hpo_analysis",
                        })
                        processed += 1
                        self._activity(
                            "run_completed",
                            self._activity_result_payload(
                                self.database.execution_request(item["id"]),
                                result,
                                operation=self._operation_for_request(
                                    self.database.execution_request(item["id"]),
                                ),
                                batch_id=batch_id,
                            ) | {
                                "total": len(claimed), "completed": processed,
                                "usage": self._dispatch_usage(dispatch),
                            },
                        )
                        continue
                    self.database.mark_awaiting_evaluation(item["id"], batch_id)
                    awaiting.append(item["id"])
                    processed += 1
                    self._activity(
                        "run_completed",
                        self._activity_result_payload(
                            self.database.execution_request(item["id"]),
                            result,
                            operation=self._operation_for_request(
                                self.database.execution_request(item["id"]),
                            ),
                            batch_id=batch_id,
                        ) | {
                            "total": len(claimed), "completed": processed,
                            "usage": self._dispatch_usage(dispatch),
                        },
                    )
                except (KeyError, TypeError, ValueError) as error:
                    self._retry_or_block(
                        item, "invalid_execution_result", str(error),
                    )
                    terminal.append({"work_item_id": item["id"], "state": "retry"})
                    processed += 1
                    request = self.database.execution_request(item["id"])
                    self._activity(
                        "run_failed",
                        self._activity_request_payload(
                            request, operation=self._operation_for_request(request),
                            batch_id=batch_id,
                        ) | {
                            "total": len(claimed), "completed": processed,
                            "blocker_code": "invalid_execution_result",
                            "detail": str(error),
                            "usage": self._dispatch_usage(dispatch),
                        },
                    )
            else:
                disposition = self.disposition_policy.classify(result)
                if disposition.route is ExecutionRoute.ANALYSIS:
                    self.failure_recorder.record(
                        item, disposition, batch_id=batch_id,
                    )
                    awaiting.append(item["id"])
                    terminal.append({
                        "work_item_id": item["id"], "state": "analysis",
                    })
                elif disposition.route is ExecutionRoute.OPERATOR:
                    self.database.transition_work_item(
                        item["id"], WorkState.BLOCKED,
                        allowed_from=(WorkState.RUNNING,),
                        blocker_code=disposition.code,
                        blocker_detail=disposition.detail,
                    )
                    terminal.append({
                        "work_item_id": item["id"], "state": "operator",
                    })
                else:
                    self._retry_or_block(
                        item, disposition.code, disposition.detail,
                        result.get("retry_after"),
                    )
                    terminal.append({
                        "work_item_id": item["id"], "state": "retry",
                    })
                processed += 1
                request = self.database.execution_request(item["id"])
                self._activity(
                    "run_failed",
                    self._activity_request_payload(
                        request, operation=self._operation_for_request(request),
                        batch_id=batch_id,
                    ) | {
                        "total": len(claimed), "completed": processed,
                        "blocker_code": disposition.code,
                        "detail": disposition.detail,
                        "usage": self._dispatch_usage(dispatch),
                    },
                )
        if not awaiting:
            return {
                "status": "batch_terminal", "batch_id": batch_id, "results": terminal,
                "recovered": recovered, "promoted": promoted,
            }
        pending = self.database.pending_batch_evaluation(self.worker_id)
        ready_for_analysis = self._analysis_ready_rows(pending)
        if not ready_for_analysis:
            self._runtime(
                "awaiting_analysis_cohort",
                batch_id=batch_id,
                detail={
                    "pending_items": len(pending),
                    "minimum_items": self.resource_policy.analysis_cohort_min,
                },
            )
            return {
                "status": "awaiting_analysis_cohort",
                "batch_id": batch_id,
                "results": terminal,
                "pending_items": len(pending),
                "minimum_items": self.resource_policy.analysis_cohort_min,
                "recovered": recovered,
                "promoted": promoted,
            }
        analysis = self._analyze_pending(
            ready_for_analysis, recovered=recovered, promoted=promoted,
        )
        analysis["execution_results"] = terminal
        if unknown:
            analysis["ignored_unknown_results"] = sorted(unknown)
        return analysis

    def _analyze_pending(
        self,
        rows: list[dict],
        *,
        recovered: int,
        promoted: int,
    ) -> dict[str, Any]:
        """Partition ordinary and HPO work into disjoint 4-8 item cohorts."""
        cohorts = self._analysis_cohorts(rows)
        self._activity(
            "analysis_started",
            {
                "stage": "analyzing",
                "count": len(rows),
                "batch_id": rows[0]["batch_id"] if rows else None,
            },
        )
        self._runtime(
            "analyzing",
            batch_id=rows[0]["batch_id"] if rows else None,
            detail={
                "analyzer_state": "running",
                "cohorts": len(cohorts),
                "experiments": len(rows),
            },
        )
        if self.resource_policy.analysis_parallelism > 1 and len(cohorts) > 1:
            with ThreadPoolExecutor(
                max_workers=min(
                    self.resource_policy.analysis_parallelism, len(cohorts),
                )
            ) as executor:
                results = list(executor.map(
                    lambda cohort: self._analyze_cohort(cohort, attempt=1),
                    cohorts,
                ))
        else:
            results = [
                self._analyze_cohort(cohort, attempt=1)
                for cohort in cohorts
            ]
        failed = [result for result in results if result["status"] != "finished"]
        evaluated = [
            item for result in results
            for item in result.get("evaluated", [])
        ]
        activity_payload = {
            "stage": "analyzing",
            "batch_id": rows[0]["batch_id"] if rows else None,
            "total": len(rows),
            "items": self._activity_analysis_items(rows),
            "evaluated": len(evaluated),
            "usage": self._merge_usage(
                result.get("usage") for result in results
            ),
        }
        if failed:
            activity_payload["detail"] = failed[0].get("detail")
            self._activity("analysis_failed", activity_payload)
        else:
            self._activity("analysis_completed", activity_payload)
        return {
            "status": "analysis_failed" if failed else "batch_complete",
            "batch_id": rows[0]["batch_id"] if rows else None,
            "cohorts": results,
            "evaluated": evaluated,
            "detail": failed[0].get("detail") if failed else None,
            "recovered": recovered,
            "promoted": promoted,
            "operator": operator_status(self.database),
        }

    def _analyze_hpo_job(
        self,
        job: dict,
        *,
        recovered: int,
        promoted: int,
    ) -> dict[str, Any]:
        """Interpret one imported/completed HPO study using canonical evidence."""
        study_id = job["study_id"]
        payload = self.database.hpo_analysis_payload(
            study_id, limit=1000,
        )
        detail = self.database.hpo_study_detail(study_id)
        self._activity(
            "analysis_started",
            {
                "stage": "hpo_analysis",
                "count": 1,
                "study_id": study_id,
            },
        )
        if not payload or not detail:
            return self._fail_hpo_job(
                job, "HPO analysis payload unavailable",
                recovered=recovered, promoted=promoted,
            )
        selected_by_number = {
            int(item["trial_number"]): item
            for item in detail.get("selected_trials", [])
        }
        trials = payload["trials"]
        if selected_by_number:
            trials = [
                trial for trial in trials
                if int(trial["trial_number"]) in selected_by_number
            ]
        canonical: list[dict[str, Any]] = []
        objective_name = payload["study"].get("objective_name") or "objective"
        for trial in trials:
            selection = selected_by_number.get(int(trial["trial_number"]), {})
            objective = trial.get("objective_value")
            trial_raw = trial.get("evidence", [])
            trial_models = [
                NormalizedEvidence.from_row(raw) for raw in trial_raw
            ]
            trial_gates = evaluate_gates(
                trial_models, policy=self.resource_policy,
            )
            classification = selection.get("classification")
            for raw in trial_raw:
                item = {
                    key: value for key, value in raw.items()
                    if value is not None
                }
                item["optimizer_objective"] = (
                    f"{objective_name}={objective}"
                    if objective is not None else objective_name
                )
                if selection.get("selection_reason"):
                    item["finding"] = (
                        f"{selection['selection_reason']} "
                        f"{trial_gates.finding}"
                    )
                item["verdict"] = (
                    "reject" if classification == "likely_overfit"
                    else "revise" if classification in {
                        "validation_candidate", "selected",
                    }
                    else trial_gates.verdict.value
                )
                canonical.append(item)
        if not canonical:
            return self._fail_hpo_job(
                job, "HPO study has no canonical completed-trial evidence",
                recovered=recovered, promoted=promoted,
            )
        experiment_id = payload["study"]["hpo_experiment_id"]
        request = {
            "schema_version": 1,
            "task_type": "analyze_hpo",
            "analysis_cohort_id": job.get("cohort_id"),
            "analyzer_timeout_seconds": (
                self.resource_policy.analyzer_timeout_seconds
            ),
            "instruction": (
                "Interpret stable regions and overfit risk already represented "
                "in canonical findings. Recommend validation only; never "
                "overwrite strategy defaults."
            ),
            "executions": [{
                "experiment_id": experiment_id,
                "evidence": canonical,
            }],
        }
        payload_bytes = len(json.dumps(
            request, separators=(",", ":"), sort_keys=True,
        ).encode())
        self._runtime(
            "hpo_analysis",
            detail={
                "analyzer_state": "running",
                "study_id": study_id,
                "job_id": job["id"],
                "attempt": job.get("attempts"),
                "payload_bytes": payload_bytes,
            },
        )
        work_item_id = payload["study"].get("hpo_work_item_id")
        if work_item_id:
            self._record_stage(
                work_item_id, "hpo_analysis", duration_ms=None,
                state="running", analyzer_attempt=job.get("attempts"),
                cohort_id=job.get("cohort_id"),
            )
        started = time.perf_counter()
        dispatch = self._dispatch(request)
        duration_ms = (time.perf_counter() - started) * 1000
        valid, error, evaluations = self._validate_analysis_response(
            dispatch, [{"experiment_id": experiment_id}],
        )
        if not valid:
            return self._fail_hpo_job(
                job, error, recovered=recovered, promoted=promoted,
                duration_ms=duration_ms, payload_bytes=payload_bytes,
                work_item_id=work_item_id,
            )
        raw = evaluations[0]
        finding = str(raw.get("finding") or "").strip()
        next_action = str(raw.get("next_action") or "").strip()
        if not finding or not next_action:
            return self._fail_hpo_job(
                job, "HPO analysis requires finding and next_action",
                recovered=recovered, promoted=promoted,
                duration_ms=duration_ms, payload_bytes=payload_bytes,
                work_item_id=work_item_id,
            )
        verdict_value = str(raw.get("verdict") or "revise")
        if verdict_value not in {
            "paper_trade_candidate", "revise", "reject",
        }:
            verdict_value = "revise"
        validation_numbers = [
            number for number, selection in selected_by_number.items()
            if selection.get("classification") in {
                "validation_candidate", "selected",
            }
        ]
        if verdict_value == "paper_trade_candidate":
            finding = (
                f"{finding} HPO promotion claim held until validation "
                "evidence is complete."
            )
            next_action = (
                f"{next_action} Do not promote before OOS and rolling "
                "validation."
            )
            verdict_value = "revise"
        self.database.add_evaluation(Evaluation(
            experiment_id=experiment_id,
            verdict=Verdict(verdict_value),
            summary=finding,
            metrics_summary=json.dumps(
                canonical, separators=(",", ":"), sort_keys=True,
            ),
            next_step=next_action,
            evaluator="ats-lab-hpo-analyzer",
        ))
        if validation_numbers:
            validations = self.database.schedule_hpo_validations(
                study_id, validation_numbers,
                evidence_splits=("oos", "rolling"),
            )
            status = "validation_scheduled"
            disposition = "revise"
        else:
            self.database.terminalize_hpo_analysis(
                job["id"], disposition=verdict_value,
                finding=finding, next_action=next_action,
            )
            validations = []
            status = "terminal"
            disposition = verdict_value
        if work_item_id:
            self._record_stage(
                work_item_id, "hpo_analysis", duration_ms=duration_ms,
                state="finished", analyzer_attempt=job.get("attempts"),
                cohort_id=job.get("cohort_id"),
            )
        self._activity(
            "analysis_completed",
            {
                "stage": "hpo_analysis",
                "total": 1,
                "evaluated": 1,
                "items": [{
                    "experiment_id": experiment_id,
                    "verdict": verdict_value,
                    "summary": finding,
                    "strategy": self._activity_strategy_for_experiment(
                        experiment_id,
                    ),
                }],
                "usage": self._dispatch_usage(dispatch),
            },
        )
        return {
            "status": status,
            "study_id": study_id,
            "analysis_job_id": job["id"],
            "attempt": job.get("attempts"),
            "payload_bytes": payload_bytes,
            "disposition": disposition,
            "validations": len(validations),
            "recovered": recovered,
            "promoted": promoted,
        }

    def _fail_hpo_job(
        self,
        job: dict,
        error: str,
        *,
        recovered: int,
        promoted: int,
        duration_ms: float | None = None,
        payload_bytes: int | None = None,
        work_item_id: str | None = None,
    ) -> dict[str, Any]:
        retry_after = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z",
        )
        state = self.database.retry_hpo_analysis(
            job["id"], error=error, retry_after=retry_after,
            max_attempts=2,
        )
        if work_item_id:
            self._record_stage(
                work_item_id, "hpo_analysis", duration_ms=duration_ms,
                state=state["state"], analyzer_attempt=job.get("attempts"),
                cohort_id=job.get("cohort_id"),
            )
        self._activity(
            "analysis_failed",
            {
                "stage": "hpo_analysis",
                "total": 1,
                "study_id": job.get("study_id"),
                "detail": error,
            },
        )
        return {
            "status": (
                "hpo_analysis_blocked"
                if state["state"] == "terminal"
                else "hpo_analysis_retry"
            ),
            "study_id": job["study_id"],
            "analysis_job_id": job["id"],
            "attempt": job.get("attempts"),
            "payload_bytes": payload_bytes,
            "detail": error,
            "recovered": recovered,
            "promoted": promoted,
        }

    def _analysis_cohorts(self, rows: list[dict]) -> list[list[dict]]:
        ordinary = [
            row for row in rows if self._operation(row) != "hpo"
        ]
        hpo = [row for row in rows if self._operation(row) == "hpo"]
        return [
            cohort
            for group in (ordinary, hpo)
            for cohort in self._balanced_chunks(group)
        ]

    def _analysis_ready_rows(self, rows: list[dict]) -> list[dict]:
        """Return pending work only when a minimum ordinary cohort is ready.

        Ordinary execution evidence accumulates across execution batches. HPO
        analysis remains independently dispatchable because it has a separate
        payload contract and can legitimately contain one imported study.
        """
        if not rows:
            return []
        ordinary = [
            row for row in rows if self._operation(row) != "hpo"
        ]
        hpo = [row for row in rows if self._operation(row) == "hpo"]
        ready_ids: set[str] = {str(row["work_item_id"]) for row in hpo}
        ready_ids.update(
            str(row["work_item_id"])
            for row in ordinary
            if self._deterministic_analysis_payload(row) is not None
        )
        if len(ordinary) >= self.resource_policy.analysis_cohort_min:
            ready_ids.update(str(row["work_item_id"]) for row in ordinary)
        return [
            row for row in rows if str(row["work_item_id"]) in ready_ids
        ]

    def _balanced_chunks(self, rows: list[dict]) -> list[list[dict]]:
        if not rows:
            return []
        maximum = self.resource_policy.analysis_cohort_max
        count = (len(rows) + maximum - 1) // maximum
        base, extra = divmod(len(rows), count)
        result = []
        offset = 0
        for index in range(count):
            size = base + (1 if index < extra else 0)
            result.append(rows[offset:offset + size])
            offset += size
        return result

    def _analyze_cohort(
        self,
        rows: list[dict],
        *,
        attempt: int,
    ) -> dict[str, Any]:
        cohort_id = (
            f"ANALYSIS-{uuid.uuid4().hex[:12].upper()}-A{attempt}"
        )
        task_type = (
            "analyze_hpo"
            if all(self._operation(row) == "hpo" for row in rows)
            else "analyze_batch"
        )
        try:
            compact_executions = [self._compact_execution(row) for row in rows]
        except (KeyError, TypeError, ValueError) as error:
            return self._analysis_failure(
                rows, cohort_id, attempt,
                f"normalized evidence unavailable: {error}",
            )

        # Lifecycle-only cohorts do not need another model turn.  Significance
        # and cost-sensitivity verdicts are already determined by canonical
        # evidence; asking Agent to restate them adds tokens without adding
        # research judgment.  Keep mixed cohorts on the model path so an
        # interpretation is still available for rows with unresolved gates.
        deterministic = [
            payload for row in rows
            if (payload := self._deterministic_analysis_payload(row)) is not None
        ]
        if len(deterministic) == len(rows):
            for row in rows:
                self._record_stage(
                    row["work_item_id"], "analysis", duration_ms=0,
                    state="running", analyzer_attempt=attempt,
                    cohort_id=cohort_id,
                )
            started = time.perf_counter()
            try:
                finalized = self._finalize_analysis(rows, deterministic)
            except (KeyError, StopIteration, TypeError, ValueError) as error:
                return self._analysis_failure(
                    rows, cohort_id, attempt, str(error),
                    duration_ms=(time.perf_counter() - started) * 1000,
                    payload_bytes=0,
                )
            duration_ms = (time.perf_counter() - started) * 1000
            for row in rows:
                self._record_stage(
                    row["work_item_id"], "analysis", duration_ms=duration_ms,
                    state="finished", analyzer_attempt=attempt,
                    cohort_id=cohort_id,
                )
            return {
                "status": "finished",
                "cohort_id": cohort_id,
                "task_type": task_type,
                "attempt": attempt,
                "payload_bytes": 0,
                "analysis_calls_avoided": 1,
                "evaluated": finalized,
                "usage": {},
            }
        request = {
            "schema_version": 1,
            "task_type": task_type,
            "analysis_cohort_id": cohort_id,
            "analyzer_timeout_seconds": (
                self.resource_policy.analyzer_timeout_seconds
            ),
            "instruction": (
                "Interpret deterministic gate results and canonical evidence. "
                "Return concise finding, disposition, and next action only."
            ),
            "executions": compact_executions,
        }
        memory = self._analyzer_advisory_memory(rows)
        request["advisory_memory"] = memory["advisory_memory"]
        request["memory_degraded"] = memory["memory_degraded"]
        payload_bytes = len(json.dumps(
            request, separators=(",", ":"), sort_keys=True,
        ).encode())
        for row in rows:
            self._record_stage(
                row["work_item_id"], "analysis", duration_ms=None,
                state="running", analyzer_attempt=attempt,
                cohort_id=cohort_id,
            )
        started = time.perf_counter()
        dispatch = self._dispatch(request)
        analysis_ms = (time.perf_counter() - started) * 1000
        valid, detail, evaluations = self._validate_analysis_response(
            dispatch, rows,
        )
        if not valid:
            if dispatch.blocker_code in INFRASTRUCTURE_BLOCKERS:
                retry_after = resolve_retry_after(
                    None, default_seconds=max(1.0, self.retry_delay_seconds),
                )
                for row in rows:
                    self._record_stage(
                        row["work_item_id"], "analysis",
                        duration_ms=analysis_ms, state="infrastructure_retry",
                        analyzer_attempt=attempt, cohort_id=cohort_id,
                    )
                    self.database.defer_batch_analysis_retry(
                        row["work_item_id"],
                        blocker_code=dispatch.blocker_code or "analyzer_failure",
                        blocker_detail=detail,
                        retry_after=retry_after,
                    )
                return {
                    "status": "infrastructure_retry",
                    "cohort_id": cohort_id, "attempt": attempt,
                    "payload_bytes": payload_bytes, "detail": detail,
                    "evaluated": [],
                    "usage": self._dispatch_usage(dispatch),
                }
            if attempt <= self.resource_policy.analyzer_retry_limit:
                retry_results = [
                    self._analyze_cohort(subset, attempt=attempt + 1)
                    for subset in self._reduced_retry_cohorts(rows)
                ]
                failed = [
                    result for result in retry_results
                    if result["status"] != "finished"
                ]
                return {
                    "status": "failed" if failed else "finished",
                    "cohort_id": cohort_id,
                    "attempt": attempt,
                    "retried": True,
                    "payload_bytes": payload_bytes,
                    "detail": failed[0].get("detail") if failed else detail,
                    "evaluated": [
                        item for result in retry_results
                        for item in result.get("evaluated", [])
                    ],
                    "usage": self._merge_usage(
                        result.get("usage") for result in retry_results
                    ),
                }
            return self._analysis_failure(
                rows, cohort_id, attempt, detail,
                duration_ms=analysis_ms, payload_bytes=payload_bytes,
            )

        try:
            finalized = self._finalize_analysis(rows, evaluations)
        except (KeyError, StopIteration, TypeError, ValueError) as error:
            if attempt <= self.resource_policy.analyzer_retry_limit:
                retry_results = [
                    self._analyze_cohort(subset, attempt=attempt + 1)
                    for subset in self._reduced_retry_cohorts(rows)
                ]
                failed = [
                    result for result in retry_results
                    if result["status"] != "finished"
                ]
                return {
                    "status": "failed" if failed else "finished",
                    "cohort_id": cohort_id,
                    "attempt": attempt,
                    "retried": True,
                    "payload_bytes": payload_bytes,
                    "detail": str(error),
                    "evaluated": [
                        item for result in retry_results
                        for item in result.get("evaluated", [])
                    ],
                    "usage": self._merge_usage(
                        result.get("usage") for result in retry_results
                    ),
                }
            return self._analysis_failure(
                rows, cohort_id, attempt, str(error),
                duration_ms=analysis_ms, payload_bytes=payload_bytes,
            )
        for row in rows:
            self._record_stage(
                row["work_item_id"], "analysis",
                duration_ms=analysis_ms, state="finished",
                analyzer_attempt=attempt, cohort_id=cohort_id,
            )
        return {
            "status": "finished",
            "cohort_id": cohort_id,
            "task_type": task_type,
            "attempt": attempt,
            "payload_bytes": payload_bytes,
            "evaluated": finalized,
            "usage": self._dispatch_usage(dispatch),
        }

    def _deterministic_analysis_payload(self, row: dict) -> dict[str, Any] | None:
        """Build a complete evaluation when lifecycle gates are authoritative."""
        if row.get("run_status") != RunStatus.FINISHED.value:
            return self._deterministic_failure_analysis_payload(row)
        try:
            evidence, gates = self._gated_evidence(row)
            verdict = self._deterministic_verdict(evidence)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if verdict is None:
            return None
        next_action = {
            Verdict.PASS: "Continue to the next validation route.",
            Verdict.INCONCLUSIVE: "Collect missing evidence before promotion.",
            Verdict.REJECT: "Archive this candidate and generate a controlled revision.",
        }[verdict]
        return {
            "experiment_id": row["experiment_id"],
            "verdict": verdict.value,
            "finding": f"Lifecycle gate: {verdict.value}. {gates.finding}",
            "next_action": next_action,
            "metrics_summary": json.dumps(
                self.analysis_input_builder.metrics_summary(row, evidence),
                separators=(",", ":"), sort_keys=True,
            ),
            "evaluator": "ats-lab-deterministic-analyzer",
        }

    def _deterministic_failure_analysis_payload(
        self, row: dict,
    ) -> dict[str, Any] | None:
        """Close terminal strategy failures without another analyzer turn."""
        try:
            raw_error = json.loads(row.get("error_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_error = {}
        if not isinstance(raw_error, dict):
            return None
        if str(raw_error.get("kind") or "") != "strategy_or_harness":
            return None
        code = str(raw_error.get("code") or "execution_failed")[:96]
        detail = " ".join(
            str(raw_error.get("detail") or "terminal execution failure").split()
        )[:240]
        revision_codes = {
            "missing_exit_framework",
            "source_strategy_not_found",
            "jesse_execution_stopped",
        }
        if code in INFRASTRUCTURE_FAILURE_CODES:
            verdict = Verdict.INFRASTRUCTURE_FAILURE
            next_action = (
                "Repair the execution infrastructure or Jesse session, then "
                "rerun; this result is not strategy evidence."
            )
        elif code in revision_codes:
            verdict = Verdict.REVISE
            next_action = "Apply one bounded harness or strategy correction, then rerun."
        else:
            # A stopped execution has no performance evidence. Keep it on the
            # correction path rather than teaching the research loop that the
            # untested strategy itself was rejected.
            verdict = Verdict.REVISE
            next_action = "Diagnose the terminal execution failure, correct one bounded issue, then rerun."
        return {
            "experiment_id": row["experiment_id"],
            "verdict": verdict.value,
            "finding": (
                f"Terminal strategy or harness failure ({code}); "
                f"no performance evidence. {detail}"
            ),
            "next_action": next_action,
            "evaluator": "ats-lab-deterministic-analyzer",
        }

    def _validate_analysis_response(
        self,
        dispatch: DispatchResult,
        rows: list[dict],
    ) -> tuple[bool, str, list[dict]]:
        payload = dispatch.payload or {}
        evaluations = payload.get("evaluations")
        if dispatch.outcome != "finished" or not isinstance(evaluations, list):
            return (
                False,
                dispatch.detail or "analyzer returned invalid result",
                [],
            )
        expected = {row["experiment_id"] for row in rows}
        actual = {
            item.get("experiment_id")
            for item in evaluations if isinstance(item, dict)
        }
        if actual != expected or len(evaluations) != len(expected):
            return (
                False,
                f"evaluation coverage mismatch: expected={sorted(expected)} "
                f"actual={sorted(value for value in actual if value)}",
                [],
            )
        return True, "", evaluations

    def _finalize_analysis(
        self,
        rows: list[dict],
        evaluations: list[dict],
    ) -> list[str]:
        validated = []
        for raw in evaluations:
            payload_item = dict(raw)
            payload_item.setdefault("evaluator", "ats-lab-batch-analyzer")
            payload_item["summary"] = payload_item.pop("finding", "")
            payload_item["next_step"] = payload_item.pop("next_action", "")
            run_row = next(
                row for row in rows
                if row["experiment_id"] == payload_item.get("experiment_id")
            )
            execution_failed = (
                run_row.get("run_status") != RunStatus.FINISHED.value
            )
            if execution_failed:
                normalized = self._normalized_evidence(run_row)
                gates = None
            else:
                normalized, gates = self._gated_evidence(run_row)
            payload_item["metrics_summary"] = json.dumps(
                self.analysis_input_builder.metrics_summary(
                    run_row, normalized,
                ),
                separators=(",", ":"), sort_keys=True,
            )
            missing = [
                name for name in ("summary", "metrics_summary", "next_step")
                if not payload_item.get(name)
            ]
            if missing:
                raise ValueError(
                    "batch evaluation missing fields: " + ", ".join(missing)
                )
            evaluation = evaluation_from_payload(payload_item)
            lifecycle_verdict = self._deterministic_verdict(normalized)
            if execution_failed:
                self.analysis_input_builder.validate_failure_verdict(
                    run_row, evaluation.verdict,
                )
            elif lifecycle_verdict is not None:
                evaluation = replace(
                    evaluation, verdict=lifecycle_verdict,
                )
            elif gates is not None and gates.failed:
                evaluation = replace(
                    evaluation, verdict=Verdict.REJECT,
                )
            # Keep baseline/significance PASS semantics unchanged.  Only an
            # explicit paper-trade claim, or PASS backed by validation-stage
            # evidence, is a promotion claim and must clear unseen-window and
            # fee-stress evidence gates.
            promotion_stage = any(
                item.evidence_split in {"oos", "rolling"}
                or item.lifecycle_stage in {"out_of_sample", "paper_trade"}
                for item in normalized
            )
            promotion_claim = (
                evaluation.verdict is Verdict.PAPER_TRADE_CANDIDATE
                or (evaluation.verdict is Verdict.PASS and promotion_stage)
            )
            gate_evidence = normalized
            if not execution_failed and (
                promotion_claim
                or evaluation.verdict is Verdict.HPO_CANDIDATE
            ):
                gate_evidence = (
                    normalized + self._machine_cost_stress_rows(run_row)
                )
            if not execution_failed and promotion_claim:
                promotion = evaluate_promotion(
                    gate_evidence, policy=self.resource_policy,
                )
                if not promotion.allowed:
                    evaluation = replace(
                        evaluation,
                        verdict=(
                            Verdict.REJECT
                            if promotion.failed else Verdict.INCONCLUSIVE
                        ),
                        summary=(
                            f"{evaluation.summary.rstrip()} "
                            f"{promotion.finding}"
                        ).strip(),
                        next_step=(
                            "Complete OOS and rolling walk-forward validation, "
                            "candle-based Monte Carlo/path robustness, and "
                            "cost-stress checks before paper-trade review."
                        ),
                    )
                self._enqueue_cost_stress(run_row)
            if not execution_failed and (
                evaluation.verdict is Verdict.HPO_CANDIDATE
            ):
                hpo_candidate = evaluate_hpo_candidate(
                    gate_evidence, policy=self.resource_policy,
                )
                if not hpo_candidate.allowed:
                    evaluation = replace(
                        evaluation,
                        verdict=(
                            Verdict.REJECT
                            if hpo_candidate.failed else Verdict.INCONCLUSIVE
                        ),
                        summary=(
                            f"{evaluation.summary.rstrip()} "
                            f"{hpo_candidate.finding}"
                        ).strip(),
                        next_step=(
                            "Satisfy the documented HPO-candidate criteria: "
                            "positive baseline after fees, activity floor per "
                            "window, multi-window positivity, no single "
                            "dominant route, and surviving fee sensitivity."
                        ),
                    )
            operation = self._operation(run_row)
            if (
                not execution_failed
                and operation == "significance"
                and lifecycle_verdict is None
            ):
                raise ValueError(
                    "significance batch evaluation requires normalized "
                    "significance_p_value"
                )
            validated.append((
                evaluation, operation, normalized, execution_failed,
                run_row["work_item_id"],
            ))

        finalized = []
        for (
            evaluation, operation, normalized, execution_failed, work_item_id,
        ) in validated:
            persistence_started = time.perf_counter()
            item = self.database.finalize_batch_evaluation(
                evaluation, work_item_id=work_item_id,
            )
            persistence_ms = (
                time.perf_counter() - persistence_started
            ) * 1000
            if execution_failed and operation == "hpo":
                study = self.database.hpo_study_for_work_item(work_item_id)
                if study is not None:
                    # A terminal optimizer failure has no trial evidence to
                    # analyze. Park the study at the external-trial handoff
                    # instead of leaving it hpo_running forever after the
                    # failure evaluation finishes the work item.
                    self.database.complete_hpo_study(
                        str(study["study_id"]),
                        require_trial_evidence=True,
                    )
            self._record_stage(
                item["id"], "persistence", duration_ms=persistence_ms,
                state="finished",
            )
            finalized.append(item["id"])
            if execution_failed:
                self.database.archive_scheduled_dependents(
                    work_item_id,
                    reason=f"parent_execution_{evaluation.verdict.value}",
                )
            if (
                evaluation.verdict is Verdict.HPO_CANDIDATE
                and operation != "hpo"
                and hasattr(self.database, "schedule_hpo_candidate")
            ):
                self.database.schedule_hpo_candidate(
                    evaluation.experiment_id, item["id"],
                    objective_name="sharpe_ratio",
                )
            if operation == "significance" and not execution_failed:
                p_values = [
                    evidence.significance_p_value for evidence in normalized
                    if evidence.significance_p_value is not None
                ]
                if p_values:
                    self.database.reconcile_significance_gate(
                        item["id"], float(max(p_values)),
                        self.resource_policy.active_ready_limit,
                        fdr_level=self.resource_policy.significance_fdr_level,
                    )
        if self.memory_adapter is not None:
            sync_memory_outbox(
                self.database, self.memory_adapter, apply=True,
                limit=max(1, len(finalized)),
            )
        return finalized

    def _machine_cost_stress_rows(self, run_row: dict) -> list:
        """Return machine-generated cost-stress evidence for this experiment."""
        return self.database.normalized_evidence_for_experiment(
            f"{run_row['experiment_id']}{MACHINE_COST_STRESS_SUFFIX}",
        )

    def _enqueue_cost_stress(self, run_row: dict) -> str | None:
        """Schedule a 2x-fee route variant through the normal execution queue.

        Promotion claims must be backed by machine-generated cost-stress
        evidence, so the stressed run is enqueued like any other backtest
        instead of trusting a self-reported cost_stress_status. Idempotent:
        one stress variant per parent experiment.
        """
        experiment = json.loads(run_row.get("experiment_json") or "{}")
        routes = experiment.get("routes")
        if not isinstance(routes, list) or not routes:
            return None
        stress_id = (
            f"{run_row['experiment_id']}{MACHINE_COST_STRESS_SUFFIX}"
        )
        if self.database.rows(
            "SELECT id FROM work_items WHERE id=?", (stress_id,),
        ):
            return None
        base_fee = experiment.get("fee_rate")
        if base_fee is None:
            base_fee = 0.0005
        self.database.upsert_experiment(ExperimentSpec(
            id=stress_id,
            strategy_name=str(experiment.get("strategy_name") or "unknown"),
            experiment_type=ExperimentType.COST_SENSITIVITY,
            hypothesis=str(experiment.get("hypothesis") or ""),
            archetype=str(experiment.get("archetype") or ""),
            target_regime=str(experiment.get("target_regime") or ""),
            failure_regime=str(experiment.get("failure_regime") or ""),
            routes=tuple(
                RouteSpec(**{
                    key: route[key] for key in (
                        "exchange", "symbol", "timeframe",
                        "start_date", "finish_date",
                    ) if key in route
                })
                for route in routes if isinstance(route, dict)
            ),
            balance=experiment.get("balance"),
            leverage=experiment.get("leverage"),
            leverage_mode=experiment.get("leverage_mode"),
            fee_rate=float(base_fee) * 2,
            parent_experiment_id=run_row["experiment_id"],
        ))
        self.database.upsert_work_item(WorkItem(
            id=stress_id, experiment_id=stress_id, priority=100,
            state=WorkState.SCHEDULED,
            specification={"operation": "cost_sensitivity"},
        ))
        return stress_id

    def _analysis_failure(
        self,
        rows: list[dict],
        cohort_id: str,
        attempt: int,
        detail: str,
        *,
        duration_ms: float | None = None,
        payload_bytes: int | None = None,
    ) -> dict[str, Any]:
        for row in rows:
            self._terminalize_analysis_failure(
                row, detail, cohort_id=cohort_id, attempt=attempt,
            )
            self._record_stage(
                row["work_item_id"], "analysis",
                duration_ms=duration_ms, state="blocked",
                analyzer_attempt=attempt, cohort_id=cohort_id,
            )
        return {
            "status": "failed",
            "cohort_id": cohort_id,
            "attempt": attempt,
            "payload_bytes": payload_bytes,
            "detail": detail,
            "evaluated": [],
        }

    def _reduced_retry_cohorts(
        self, rows: list[dict],
    ) -> list[list[dict]]:
        if len(rows) <= 1:
            return [rows]
        midpoint = (len(rows) + 1) // 2
        return [rows[:midpoint], rows[midpoint:]]

    def _synthesize(
        self,
        cohort: dict,
        *,
        recovered: int,
        promoted: int,
    ) -> dict[str, Any]:
        self._activity(
            "synthesis_started",
            {
                "stage": "synthesizing",
                "cohort_id": cohort["id"],
                "requested": cohort["requested_count"],
            },
        )
        self._runtime(
            "synthesizing",
            detail={"cohort_id": cohort["id"], "requested": cohort["requested_count"]},
        )
        context = build_batch_context(
            self.database, policy=self.resource_policy,
            memory_adapter=self.memory_adapter,
        )
        dispatch = self._dispatch({
            "schema_version": 1,
            "task_type": "synthesize_batch",
            "cohort": cohort,
            "context": context,
        })
        payload = dispatch.payload or {}
        evidence = payload.get("evidence")
        requests = (
            evidence.get("synthesis_requests")
            if isinstance(evidence, dict) else None
        )
        if dispatch.outcome != "finished" or not isinstance(requests, list):
            detail = dispatch.detail or "synthesis requires evidence.synthesis_requests"
            self.database.fail_synthesis_cohort(cohort["id"], detail)
            self._activity(
                "synthesis_failed",
                {
                    "stage": "synthesizing",
                    "cohort_id": cohort["id"],
                    "requested": cohort["requested_count"],
                    "detail": detail,
                    "usage": self._dispatch_usage(dispatch),
                },
            )
            return {
                "status": "synthesis_failed", "detail": detail,
                "recovered": recovered, "promoted": promoted,
            }
        try:
            bounded_requests = self._bounded_synthesis_requests(
                requests, cohort["requested_count"],
            )
            synthesis = apply_batch(
                self.database, bounded_requests, policy=self.resource_policy,
                cohort_id=cohort["id"], source_path="batch-supervisor",
            )
            if (
                synthesis["rejected"]
                or len(synthesis["generated"]) != cohort["requested_count"]
            ):
                raise ValueError(f"incomplete synthesis cohort: {synthesis}")
            chains = []
            for generated in synthesis["generated"]:
                work_item_ids = [
                    *generated.get("significance_jobs", []),
                    generated["baseline_job"],
                ]
                chains.append({
                    "slot": generated["cohort_slot"],
                    "lane": generated["lane"],
                    "source_experiment_id": generated["source_experiment_id"],
                    "work_item_ids": work_item_ids,
                })
            self.database.activate_synthesis_cohort(cohort["id"], chains)
            items = [
                {
                    key: item.get(key)
                    for key in (
                        "lane", "strategy_name", "hypothesis", "thesis",
                        "entry_rule_summary", "why_this_now", "routes",
                    )
                    if item.get(key) is not None
                }
                for item in bounded_requests
                if isinstance(item, dict)
            ]
            self._activity(
                "synthesis_completed",
                {
                    "stage": "synthesizing",
                    "cohort_id": cohort["id"],
                    "requested": cohort["requested_count"],
                    "items": items,
                    "usage": self._dispatch_usage(dispatch),
                },
            )
        except (KeyError, TypeError, ValueError) as error:
            self.database.fail_synthesis_cohort(cohort["id"], str(error))
            self._activity(
                "synthesis_failed",
                {
                    "stage": "synthesizing",
                    "cohort_id": cohort["id"],
                    "requested": cohort["requested_count"],
                    "detail": str(error),
                    "usage": self._dispatch_usage(dispatch),
                },
            )
            return {
                "status": "synthesis_failed", "detail": str(error),
                "recovered": recovered, "promoted": promoted,
            }
        return {
            "status": "synthesized", "synthesis": synthesis,
            "over_generated": len(requests) - len(bounded_requests),
            "recovered": recovered, "promoted": promoted,
        }

    def _bounded_synthesis_requests(
        self,
        requests: list[dict[str, Any]],
        requested_count: int,
    ) -> list[dict[str, Any]]:
        """Trim over-generation deterministically while preserving lane gates."""
        received = len(requests)
        if received < requested_count:
            raise ValueError(
                f"synthesis cohort returned {received}/{requested_count} requests"
            )
        if received == requested_count:
            return requests
        indexed = list(enumerate(requests))
        if any(
            not isinstance(item, dict)
            or item.get("lane") not in {"new_concept", "improvement"}
            for _, item in indexed
        ):
            raise ValueError(
                "over-generated synthesis cohort contains invalid lane"
            )
        improvements = [
            pair for pair in indexed if pair[1]["lane"] == "improvement"
        ]
        new_concepts = [
            pair for pair in indexed if pair[1]["lane"] == "new_concept"
        ]
        maximum_improvements = min(
            self.resource_policy.synthesis_max_improvements,
            requested_count - self.resource_policy.synthesis_min_new_concepts,
        )
        improvement_count = min(
            len(improvements), maximum_improvements,
        )
        new_count = requested_count - improvement_count
        if len(new_concepts) < new_count:
            raise ValueError(
                "over-generated synthesis cohort cannot satisfy lane policy"
            )
        selected = {
            index for index, _ in improvements[:improvement_count]
        } | {
            index for index, _ in new_concepts[:new_count]
        }
        return [
            item for index, item in indexed if index in selected
        ]

    def _reserve_cohort(self) -> dict | None:
        return self.database.reserve_synthesis_cohort(
            worker_id=self.worker_id,
            requested_count=self.resource_policy.synthesis_generate_limit,
            low_watermark=self.resource_policy.synthesis_low_watermark,
            lease_seconds=self.resource_policy.synthesis_lease_seconds,
            retry_cooldown_seconds=self.resource_policy.synthesis_retry_cooldown_seconds,
        )

    def _dispatch(self, request: dict[str, Any]) -> DispatchResult:
        runtime = self.database.supervisor_runtime_status() or {}
        heartbeat = RuntimeHeartbeat(
            self.database,
            worker_id=self.worker_id,
            started_at=self.started_at,
            phase=str(runtime.get("phase") or request.get("task_type") or "dispatching"),
            batch_id=runtime.get("batch_id"),
            detail=runtime.get("detail"),
            interval_seconds=self.heartbeat_interval_seconds,
        )
        try:
            with heartbeat:
                return self.dispatcher.dispatch(request)
        except Exception as error:
            return DispatchResult(
                outcome="retry", blocker_code="dispatcher_exception", detail=str(error),
            )

    def _runtime(
        self,
        phase: str,
        *,
        batch_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.database.update_supervisor_runtime(
            worker_id=self.worker_id,
            process_id=os.getpid(),
            phase=phase,
            batch_id=batch_id,
            detail=detail,
            started_at=self.started_at,
        )

    def _activity(
        self, event_type: str, payload: dict[str, Any],
    ) -> None:
        """Append optional operator activity without affecting research state."""
        recorder = getattr(self.database, "record_event", None)
        if recorder is None:
            return
        try:
            recorder(
                "supervisor", self.worker_id, event_type,
                {key: value for key, value in payload.items() if value is not None},
            )
        except Exception:
            # Activity is an operator convenience. SQLite workflow state wins.
            return

    def _activity_request_payload(
        self, request: dict[str, Any], *, operation: str,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        experiment = request.get("experiment")
        if not isinstance(experiment, dict):
            experiment = {}
        routes = experiment.get("routes")
        if not isinstance(routes, list):
            routes = []
        return {
            "operation": operation,
            "batch_id": batch_id,
            "work_item_id": request.get("work_item_id"),
            "experiment_id": request.get("experiment_id"),
            "strategy": experiment.get("strategy_name") or "unknown",
            "hypothesis": experiment.get("hypothesis") or "",
            "thesis": experiment.get("thesis") or "",
            "entry_rule_summary": experiment.get("entry_rule_summary") or "",
            "routes": [route for route in routes if isinstance(route, dict)][:4],
            "success_gates": [
                gate for gate in (experiment.get("success_gates") or [])
                if isinstance(gate, dict)
            ],
        }

    def _activity_result_payload(
        self,
        request: dict[str, Any],
        result: dict[str, Any],
        *,
        operation: str,
        batch_id: str | None = None,
    ) -> dict[str, Any]:
        payload = self._activity_request_payload(
            request, operation=operation, batch_id=batch_id,
        )
        evidence = result.get("evidence")
        run = evidence.get("run") if isinstance(evidence, dict) else None
        if not isinstance(run, dict):
            run = {}
        for key in ("route", "dashboard_url", "metrics", "status"):
            if run.get(key) is not None:
                payload[key] = run[key]
        route = payload.get("route")
        if isinstance(route, dict) and isinstance(route.get("routes"), list):
            payload["routes"] = route["routes"]
        metric_states = self._activity_metric_states(
            payload.get("metrics"), payload.get("success_gates"),
        )
        if metric_states:
            payload["metric_states"] = metric_states
        return payload

    def _activity_metric_states(
        self,
        metrics: object,
        gates: object,
    ) -> dict[str, str]:
        if not isinstance(metrics, dict) or not isinstance(gates, list):
            return {}
        aliases = {
            "trades": "trade_count",
            "trade_count": "trade_count",
            "net": "net_profit_percentage",
            "net_profit_percentage": "net_profit_percentage",
            "sharpe": "sharpe_ratio",
            "sharpe_ratio": "sharpe_ratio",
            "max_dd": "max_drawdown_percentage",
            "max_drawdown_percentage": "max_drawdown_percentage",
        }
        states: dict[str, str] = {}
        for gate in gates:
            if not isinstance(gate, dict):
                continue
            metric_name = aliases.get(str(gate.get("name") or ""))
            if not metric_name or metric_name not in metrics:
                continue
            try:
                value = float(metrics[metric_name])
                threshold = float(gate["threshold"])
            except (KeyError, TypeError, ValueError):
                continue
            operator = str(gate.get("operator") or ">=")
            passed = {
                ">": value > threshold,
                ">=": value >= threshold,
                "<": value < threshold,
                "<=": value <= threshold,
                "=": value == threshold,
                "==": value == threshold,
            }.get(operator)
            if passed is None:
                continue
            distance = abs(value - threshold)
            warning_band = max(abs(threshold) * 0.10, 0.10)
            states[metric_name] = (
                "yellow" if distance <= warning_band
                else "green" if passed else "red"
            )
        return states

    def _operation_for_request(self, request: dict[str, Any]) -> str:
        work_item = request.get("work_item")
        if isinstance(work_item, dict) and work_item.get("operation"):
            return str(work_item["operation"])
        experiment = request.get("experiment")
        experiment_type = (
            experiment.get("experiment_type")
            if isinstance(experiment, dict) else None
        )
        return {
            "baseline": "backtest",
            "multi_window": "backtest",
            "cost_sensitivity": "backtest",
            "out_of_sample": "backtest",
            "harness_check": "backtest",
            "significance": "significance",
            "monte_carlo": "monte_carlo",
            "hpo": "hpo",
        }.get(str(experiment_type or ""), "backtest")

    @staticmethod
    def _dispatch_usage(dispatch: DispatchResult) -> dict[str, int]:
        payload = dispatch.payload or {}
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return {}
        result: dict[str, int] = {}
        for key in (
            "input_tokens", "output_tokens", "cache_read_tokens", "total_tokens",
        ):
            try:
                if usage.get(key) is not None:
                    result[key] = max(0, int(usage[key]))
            except (TypeError, ValueError):
                continue
        if "total_tokens" not in result and result:
            result["total_tokens"] = (
                result.get("input_tokens", 0) + result.get("output_tokens", 0)
            )
        return result

    @staticmethod
    def _merge_usage(usages: object) -> dict[str, int]:
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "total_tokens": 0,
        }
        present = False
        if usages is None:
            return {}
        for usage in usages:
            if not isinstance(usage, dict):
                continue
            present = True
            for key in totals:
                try:
                    totals[key] += max(0, int(usage.get(key) or 0))
                except (TypeError, ValueError):
                    continue
        return totals if present else {}

    def _activity_analysis_items(self, rows: list[dict]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                evaluations = self.database.rows(
                    """SELECT ev.verdict,ev.summary,ev.next_step,s.name AS strategy
                       FROM evaluations ev
                       JOIN experiments e ON e.id=ev.experiment_id
                       LEFT JOIN strategies s ON s.id=e.strategy_id
                       WHERE ev.experiment_id=?
                       ORDER BY ev.evaluated_at DESC,ev.id DESC LIMIT 1""",
                    (row["experiment_id"],),
                )
            except Exception:
                evaluations = []
            evaluation = evaluations[0] if evaluations else {}
            items.append({
                "experiment_id": row.get("experiment_id"),
                "strategy": evaluation.get("strategy") or row.get("strategy") or "unknown",
                "verdict": evaluation.get("verdict") or "inconclusive",
                "summary": evaluation.get("summary") or "",
                "next_action": evaluation.get("next_step") or "",
            })
        return items

    def _activity_strategy_for_experiment(self, experiment_id: str) -> str:
        try:
            rows = self.database.rows(
                """SELECT s.name AS strategy FROM experiments e
                   LEFT JOIN strategies s ON s.id=e.strategy_id WHERE e.id=?""",
                (experiment_id,),
            )
        except Exception:
            rows = []
        return str(rows[0].get("strategy") or "unknown") if rows else "unknown"

    def _persist_run(self, request: dict[str, Any], result: dict[str, Any]) -> None:
        evidence = result.get("evidence")
        if not isinstance(evidence, dict) or not isinstance(evidence.get("run"), dict):
            raise ValueError("finished batch result requires evidence.run")
        run = evidence["run"]
        session_id = str(run.get("session_id") or "")
        metrics = run.get("metrics")
        raw_result = run.get("raw_result")
        if not session_id:
            raise ValueError("evidence.run.session_id is required")
        if not isinstance(metrics, dict):
            raise ValueError("evidence.run.metrics must be an object")
        if not isinstance(raw_result, dict):
            raise ValueError(
                "evidence.run.raw_result must be a compact session envelope"
            )
        expected_raw_keys = {"session_id", "status", "metrics"}
        if set(raw_result) != expected_raw_keys:
            raise ValueError(
                "evidence.run.raw_result must contain exactly "
                "session_id, status, and metrics"
            )
        raw_session_id = raw_result.get("session_id")
        if raw_session_id != session_id:
            raise ValueError(
                "evidence.run.session_id must equal raw_result.session_id"
            )
        status = str(run.get("status", "finished"))
        if raw_result.get("status") != status:
            raise ValueError(
                "evidence.run.status must equal raw_result.status"
            )
        raw_metrics = raw_result.get("metrics")
        if not isinstance(raw_metrics, dict):
            raise ValueError("evidence.run.raw_result.metrics must be an object")
        if metrics != raw_metrics:
            raise ValueError(
                "evidence.run.metrics must equal raw_result.metrics"
            )
        route_payload = run.get("route")
        route = None
        if isinstance(route_payload, dict):
            route = RouteSpec(**{
                key: route_payload[key]
                for key in ("exchange", "symbol", "timeframe", "start_date", "finish_date")
                if key in route_payload
            })
        elif status == RunStatus.FINISHED.value:
            requested_routes = request.get("experiment", {}).get("routes")
            if isinstance(requested_routes, list) and requested_routes:
                route = {
                    "coverage": "aggregate_requested_routes",
                    "evidence": {
                        "session_id": session_id,
                        "status": status,
                    },
                    "routes": [
                        {
                            key: requested[key]
                            for key in (
                                "exchange", "symbol", "timeframe",
                                "start_date", "finish_date",
                            )
                            if key in requested
                        }
                        for requested in requested_routes
                        if isinstance(requested, dict)
                    ],
                }
        self.database.add_run(RunResult(
            id=str(run.get("id") or f"{request['work_item_id']}:{session_id}"),
            experiment_id=request["experiment_id"], work_item_id=request["work_item_id"],
            session_id=session_id, status=RunStatus(status),
            route=route, dashboard_url=run.get("dashboard_url"), metrics=metrics,
            raw_result=raw_result, error=run.get("error"),
            started_at=run.get("started_at"),
            finished_at=run.get("finished_at"),
        ))

    def _retry_or_block(
        self, item: dict, code: str, detail: str, retry_after: str | None = None,
    ) -> None:
        if code in INFRASTRUCTURE_BLOCKERS:
            when = resolve_retry_after(
                retry_after, default_seconds=self.retry_delay_seconds,
            )
            self.database.defer_infrastructure_retry(
                item["id"], blocker_code=code,
                blocker_detail=detail, retry_after=when,
            )
            return
        next_attempt = int(item["attempts"]) + 1
        if next_attempt >= self.max_attempts:
            self.database.transition_work_item(
                item["id"], WorkState.BLOCKED, allowed_from=(WorkState.RUNNING,),
                blocker_code="retry_limit_reached",
                blocker_detail=f"{code} after {next_attempt} attempts: {detail}".strip(),
            )
            return
        delay = self.retry_delay_seconds * (2 ** max(0, next_attempt - 1))
        when = resolve_retry_after(retry_after, default_seconds=delay)
        self.database.transition_work_item(
            item["id"], WorkState.WAITING_RETRY, allowed_from=(WorkState.RUNNING,),
            blocker_code=code, blocker_detail=detail, retry_after=when,
        )

    def _normalized_evidence(self, row: dict) -> list[NormalizedEvidence]:
        evidence = self.database.normalized_evidence_for_run(row["run_id"])
        if not evidence:
            raise ValueError(f"run {row['run_id']} produced no normalized evidence")
        deterministic_verdict = self._deterministic_verdict(evidence)
        if deterministic_verdict is not None:
            evidence = [
                replace(item, verdict=deterministic_verdict)
                for item in evidence
            ]
        return evidence

    def _gated_evidence(
        self, row: dict,
    ) -> tuple[list[NormalizedEvidence], GateDecision]:
        evidence = self._normalized_evidence(row)
        experiment = json.loads(row.get("experiment_json") or "{}")
        routes = experiment.get("routes")
        if not isinstance(routes, list):
            routes = []
        route_payload = json.loads(row.get("route_json") or "{}")
        observed_routes = []
        if (
            route_payload.get("coverage") == "aggregate_requested_routes"
            and route_payload.get("evidence", {}).get("session_id")
            == row.get("session_id")
            and route_payload.get("evidence", {}).get("status") == "finished"
            and isinstance(route_payload.get("routes"), list)
        ):
            observed_routes = route_payload["routes"]
        gates = evaluate_gates(
            evidence, policy=self.resource_policy, expected_routes=routes,
            observed_routes=observed_routes,
        )
        lifecycle_verdict = self._deterministic_verdict(evidence)
        verdict = lifecycle_verdict or gates.verdict
        return [
            replace(item, verdict=verdict, finding=gates.finding)
            for item in evidence
        ], gates

    def _compact_execution(self, row: dict) -> dict[str, Any]:
        evidence = (
            self._gated_evidence(row)[0]
            if row.get("run_status") == RunStatus.FINISHED.value
            else self._normalized_evidence(row)
        )
        return self.analysis_input_builder.build(row, evidence)

    def _analyzer_advisory_memory(self, rows: list[dict]) -> dict[str, Any]:
        """Recall bounded, untrusted hints for model analysis only.

        Canonical SQLite evidence remains in ``executions`` and is the only
        source used by deterministic gates and persisted evaluations. Memory
        outage or malformed recall degrades to an empty hint block.
        """
        if self.memory_adapter is None:
            return {"advisory_memory": [], "memory_degraded": True}
        candidates: list[dict[str, Any]] = []
        for row in rows:
            try:
                specification = json.loads(row.get("experiment_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                specification = {}
            if not isinstance(specification, dict):
                specification = {}
            candidates.append({
                "source_experiment_id": row.get("experiment_id"),
                "strategy": specification.get("strategy_name"),
                "archetype": specification.get("archetype"),
                "target_regime": specification.get("target_regime"),
                "failure_regime": specification.get("failure_regime"),
                "specification_json": row.get("experiment_json"),
            })
        return compact_advisory_memory(
            self.memory_adapter,
            {"improvement_candidates": candidates,
             "scheduled_candidates": [], "concept_learnings": []},
            max_items=ANALYZER_MEMORY_MAX_ITEMS,
            max_bytes=ANALYZER_MEMORY_MAX_BYTES,
            max_text_chars=ANALYZER_MEMORY_MAX_TEXT_CHARS,
            max_queries=3,
            stop_on_failure=True,
        )

    def _operation(self, row: dict) -> str:
        work = self.database.rows(
            "SELECT specification_json FROM work_items WHERE id=?",
            (row["work_item_id"],),
        )
        work_spec = json.loads(work[0]["specification_json"] or "{}") if work else {}
        operation = work_spec.get("operation")
        if operation:
            return str(operation)
        experiment = json.loads(row.get("experiment_json") or "{}")
        return {
            "baseline": "backtest",
            "multi_window": "backtest",
            "cost_sensitivity": "backtest",
            "out_of_sample": "backtest",
            "harness_check": "backtest",
            "significance": "significance",
            "monte_carlo": "monte_carlo",
            "hpo": "hpo",
        }.get(experiment.get("experiment_type"), "backtest")

    def _terminalize_analysis_failure(
        self,
        row: dict,
        detail: str,
        *,
        cohort_id: str,
        attempt: int,
    ) -> None:
        summary = (
            f"Analyzer failed after {attempt} attempts in {cohort_id}: {detail}"
        )
        self.database.add_evaluation(Evaluation(
            experiment_id=row["experiment_id"],
            verdict=Verdict.INFRASTRUCTURE_FAILURE,
            summary=summary,
            metrics_summary=json.dumps(
                [
                    item.to_compact_dict()
                    for item in self._normalized_evidence(row)
                ],
                separators=(",", ":"), sort_keys=True,
            ),
            next_step=(
                "Inspect analyzer blocker; requeue durable evidence without "
                "rerunning execution."
            ),
            evaluator="ats-lab-analyzer-terminal",
        ))
        self.database.transition_work_item(
            row["work_item_id"], WorkState.BLOCKED,
            allowed_from=(WorkState.RUNNING,),
            blocker_code="analyzer_retry_exhausted",
            blocker_detail=summary,
        )

    def _record_stage(
        self,
        work_item_id: str,
        stage: str,
        *,
        duration_ms: float | None,
        state: str,
        analyzer_attempt: int | None = None,
        cohort_id: str | None = None,
    ) -> None:
        recorder = getattr(self.database, "record_work_item_stage", None)
        if recorder is None:
            return
        finished = datetime.now(timezone.utc)
        started = (
            finished - timedelta(milliseconds=duration_ms)
            if duration_ms is not None else finished
        )
        recorder(
            work_item_id=work_item_id,
            stage=stage,
            started_at=started.isoformat().replace("+00:00", "Z"),
            finished_at=(
                finished.isoformat().replace("+00:00", "Z")
                if duration_ms is not None else None
            ),
            duration_ms=(
                round(duration_ms) if duration_ms is not None else None
            ),
            state=state,
            analyzer_attempt=analyzer_attempt,
            cohort_id=cohort_id,
        )

    @staticmethod
    def _timestamp_delta_ms(
        started_at: str | None,
        finished_at: str | None,
    ) -> float | None:
        if not started_at or not finished_at:
            return None
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return max(0.0, (finished - started).total_seconds() * 1000)

    @staticmethod
    def _deterministic_verdict(
        evidence: list[NormalizedEvidence],
    ) -> Verdict | None:
        """Apply lifecycle gates to canonical fields before Agent analysis."""
        p_values = [
            item.significance_p_value for item in evidence
            if item.lifecycle_stage == "significance"
            and item.significance_p_value is not None
        ]
        if p_values:
            worst = max(p_values)
            return (
                Verdict.PASS if worst < 0.05
                else (
                    Verdict.INCONCLUSIVE if worst <= 0.10
                    else Verdict.REJECT
                )
            )
        cost_statuses = [
            item.cost_stress_status for item in evidence
            if item.lifecycle_stage == "cost_sensitivity"
            and item.cost_stress_status is not None
        ]
        if cost_statuses:
            if "fail" in cost_statuses:
                return Verdict.REJECT
            if "inconclusive" in cost_statuses:
                return Verdict.INCONCLUSIVE
            if all(status == "pass" for status in cost_statuses):
                return Verdict.PASS
        return None

    def run(
        self,
        *,
        continuous: bool,
        idle_sleep: float,
        max_rounds: int | None = None,
        on_result: Callable[[dict[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        rounds = 0
        self.database.repair_relative_retry_schedules()
        self._runtime("starting")
        self._activity(
            "research_started",
            {"stage": "starting", "worker_id": self.worker_id},
        )
        while True:
            result = self.run_round()
            rounds += 1
            if on_result:
                on_result(result)
            if not continuous or max_rounds is not None:
                results.append(result)
            if max_rounds is not None and rounds >= max_rounds:
                self._runtime("stopped", detail={"reason": "max_rounds"})
                return results
            if not continuous:
                self._runtime("stopped", detail={"reason": "single_round"})
                return results
            if result["status"] == "stop_requested":
                self._runtime("stopped", detail={"reason": "operator_request"})
                return results
            if result["status"] == "paused":
                self._runtime("paused")
            elif result["status"] == "awaiting_analysis_cohort":
                self._runtime(
                    "awaiting_analysis_cohort",
                    batch_id=result.get("batch_id"),
                    detail={
                        "pending_items": result.get("pending_items"),
                        "minimum_items": result.get("minimum_items"),
                    },
                )
            else:
                self._runtime("idle", detail={"last_status": result["status"]})
            if result["status"] in {
                "idle", "paused", "analysis_failed", "synthesis_failed",
                "awaiting_analysis_cohort",
            }:
                self.sleep(idle_sleep)
