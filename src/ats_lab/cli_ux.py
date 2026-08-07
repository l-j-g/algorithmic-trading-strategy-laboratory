"""Small human-first projections for the ATS Lab operator CLI."""
from __future__ import annotations

from typing import Any, Mapping


ROOT_HELP = """\
ATS Lab - supervised Jesse strategy research

START HERE
  ats-lab                         Show health, queue, memory, and next command
  ats-lab doctor                  Check Docker, Jesse, Memory, and workflow state
  ats-lab next                    Show one recommended next action
  ats-lab tui                     Open color keyboard-driven operator UI
  ats-lab monitor --watch         Stream plain-text progress

DAILY OPERATION
  ats-lab loop start              Start or resume canonical research loop
  ats-lab loop status             Show actual process and control state
  ats-lab loop pause              Pause loop without ending process
  ats-lab loop stop               Gracefully stop loop process
  ats-lab control status          Show supervisor control state
  ats-lab control pause|resume|stop
  ats-lab queue [--state STATE]   Inspect work
  ats-lab candidates              Show promotion/revision candidates
  ats-lab evidence                Show comparable normalized evidence

RESEARCH MEMORY
  ats-lab memory init             One-time safe historical initialization
  ats-lab memory status           Show delivery state
  ats-lab memory sync             Drain queued learnings

ANALYSIS
  ats-lab hpo                     Show optimization lifecycle
  ats-lab hpo --doctor            Show route gates and next HPO action
  ats-lab hpo-defaults            Show bootstrap route policy
  ats-lab hpo-defaults --apply    Apply it to untouched scheduled studies
  ats-lab hpo-import STUDY ...    Attach external Optuna trials and resume
  ats-lab analyzer                Show analyzer state
  ats-lab timings                 Show stage durations
  ats-lab dashboard               Open local read-only dashboard server

RECOVERY AND AUTOMATION
  ats-lab recover-claims          Preview stale claim recovery
  ats-lab recovery-audit          Classify retry and blocker candidates
  ats-lab status --format json    Machine-readable operator state

Use ats-lab <command> --help for command options.
Raw diagnostic and compatibility commands remain available for scripts.
"""


def next_guidance(
    snapshot: Mapping[str, Any], memory: Mapping[str, int],
) -> dict[str, str]:
    """Return one safe, concrete operator action."""
    control = snapshot.get("control") or {}
    runtime = snapshot.get("supervisor") or {}
    states = snapshot.get("work_states") or {}
    active = sum(int(states.get(key, 0) or 0) for key in (
        "scheduled", "ready", "running", "waiting_retry",
    ))
    if int(snapshot.get("unresolved_execution_claims", 0) or 0):
        return {
            "action": "Inspect stale execution claims",
            "reason": "running claims have exceeded the recovery threshold",
            "command": "ats-lab recover-claims",
        }
    invalid_retries = int(snapshot.get("invalid_retry_schedules", 0) or 0)
    if invalid_retries:
        return {
            "action": "Start loop and repair retry schedules",
            "reason": f"{invalid_retries} relative retry values cannot mature",
            "command": "ats-lab loop start",
        }
    if not snapshot.get("healthy", True):
        return {
            "action": "Inspect workflow health",
            "reason": "canonical workflow state needs operator attention",
            "command": "ats-lab doctor",
        }
    desired = str(control.get("desired_state") or "running")
    if desired == "paused":
        return {
            "action": "Resume supervisor control",
            "reason": "research control is paused",
            "command": "ats-lab loop start",
        }
    phase = str(runtime.get("phase") or "not_reported")
    if desired == "stop_requested" and active:
        return {
            "action": "Resume research control",
            "reason": "work remains but supervisor stop is requested",
            "command": "ats-lab loop start",
        }
    if int(memory.get("retry", 0) or 0):
        return {
            "action": "Retry advisory-memory delivery",
            "reason": "research memory has retryable delivery records",
            "command": "ats-lab memory sync",
        }
    if int(memory.get("pending", 0) or 0):
        return {
            "action": "Deliver queued research memory",
            "reason": "research memory is waiting for delivery",
            "command": "ats-lab memory sync",
        }
    if not any(int(memory.get(key, 0) or 0) for key in (
        "pending", "retry", "delivered",
    )):
        return {
            "action": "Initialize advisory research memory",
            "reason": "no research memory has been initialized",
            "command": "ats-lab memory init",
        }
    hpo = snapshot.get("hpo") or {}
    route_readiness = hpo.get("route_readiness") or {}
    if route_readiness.get("missing_route_studies"):
        missing = route_readiness.get("missing_routes") or {}
        splits = ",".join(
            split for split in ("hpo", "oos", "rolling")
            if int(missing.get(split, 0) or 0)
        )
        return {
            "action": "Configure HPO validation routes",
            "reason": f"HPO studies are held until routes exist ({splits})",
            "command": "ats-lab hpo --doctor",
        }
    waiting_retry = int(states.get("waiting_retry", 0) or 0)
    if (
        waiting_retry
        and not int(states.get("ready", 0) or 0)
        and not int(states.get("running", 0) or 0)
    ):
        return {
            "action": "Inspect delayed execution retries",
            "reason": (
                f"{waiting_retry} retry jobs hold active capacity while no work runs"
            ),
            "command": "ats-lab queue --state waiting_retry",
        }
    next_action = str(snapshot.get("next_action") or "idle")
    if next_action == "resume_batch_analysis":
        return {
            "action": "Resume batch analysis",
            "reason": "finished execution evidence awaits evaluation",
            "command": "ats-lab loop start",
        }
    if next_action in {"monitor_running_batch", "execute_batch"}:
        if phase in {"stopped", "not_reported"}:
            return {
                "action": "Start the canonical supervisor",
                "reason": "runnable research work exists without an active supervisor",
                "command": "ats-lab loop start",
            }
        return {
            "action": "Monitor the running research batch",
            "reason": "the supervisor is processing runnable work",
            "command": "ats-lab monitor --watch",
        }
    if next_action == "promote_or_resolve_dependencies":
        return {
            "action": "Inspect scheduled dependencies",
            "reason": "scheduled work is not currently promotable",
            "command": "ats-lab queue --state scheduled",
        }
    if next_action == "synthesize_cohort":
        return {
            "action": "Run cohort replenishment",
            "reason": "runnable research chains reached the synthesis watermark",
            "command": "ats-lab loop start",
        }
    return {
        "action": "Review strongest candidates",
        "reason": "workflow has no immediate execution blocker",
        "command": "ats-lab candidates",
    }


def render_guidance(guidance: Mapping[str, str]) -> str:
    return "\n".join((
        f"NEXT  {guidance['action']}",
        f"WHY  {guidance['reason']}",
        f"RUN   {guidance['command']}",
    ))


def render_home(
    snapshot: Mapping[str, Any], memory: Mapping[str, int],
) -> str:
    states = snapshot.get("work_states") or {}
    control = snapshot.get("control") or {}
    runtime = snapshot.get("supervisor") or {}
    guidance = next_guidance(snapshot, memory)
    health = "HEALTHY" if snapshot.get("healthy", True) else "ATTENTION"
    queue = "  ".join(
        f"{name}={int(states.get(name, 0) or 0)}"
        for name in ("ready", "running", "waiting_retry", "scheduled", "blocked")
    )
    memory_state = (
        "ready" if not memory.get("pending") and not memory.get("retry")
        else "attention"
    )
    return "\n".join((
        f"ATS LAB  {health}",
        (
            f"SUPERVISOR {runtime.get('phase') or 'not_reported'}  "
            f"CONTROL {control.get('desired_state') or 'running'}"
        ),
        f"QUEUE  {queue}",
        (
            f"MEMORY {memory_state}  delivered={int(memory.get('delivered', 0))}  "
            f"pending={int(memory.get('pending', 0))}  "
            f"retry={int(memory.get('retry', 0))}"
        ),
        "",
        render_guidance(guidance),
        "",
        "TUI  ats-lab tui    DETAIL  ats-lab status    HELP  ats-lab help",
    ))


def render_doctor(
    preflight: Mapping[str, Any], snapshot: Mapping[str, Any],
    memory: Mapping[str, int],
) -> str:
    lines = ["ATS LAB DOCTOR"]
    for check in preflight.get("checks", []):
        status = str(check.get("status") or "unknown")
        marker = "OK" if status == "healthy" else (
            "WARN" if status == "degraded" else "FAIL"
        )
        lines.append(f"[{marker}] {check.get('name')}")
    workflow_marker = "OK" if snapshot.get("healthy", True) else "FAIL"
    lines.append(f"[{workflow_marker}] canonical_workflow")
    memory_marker = "OK" if not memory.get("pending") and not memory.get("retry") else "WARN"
    lines.append(
        f"[{memory_marker}] research_memory "
        f"delivered={int(memory.get('delivered', 0))} "
        f"pending={int(memory.get('pending', 0))} "
        f"retry={int(memory.get('retry', 0))}"
    )
    if preflight.get("detail"):
        lines.extend(("", f"BLOCKER  {preflight['detail']}"))
    lines.extend(("", render_guidance(next_guidance(snapshot, memory))))
    return "\n".join(lines)


def render_memory_status(memory: Mapping[str, int]) -> str:
    pending = int(memory.get("pending", 0) or 0)
    retry = int(memory.get("retry", 0) or 0)
    delivered = int(memory.get("delivered", 0) or 0)
    state = "READY" if pending == 0 and retry == 0 else "ATTENTION"
    lines = [
        f"MEMORY {state}",
        f"delivered={delivered}  pending={pending}  retry={retry}",
    ]
    if retry:
        lines.append("RUN  ats-lab memory sync  # retry due deliveries")
    elif pending:
        lines.append("RUN  ats-lab memory sync  # deliver queued findings")
    elif delivered == 0:
        lines.append("RUN  ats-lab memory init  # initialize historical findings")
    else:
        lines.append("NEXT no action; future findings sync during supervision")
    return "\n".join(lines)


def render_memory_init(result: Mapping[str, Any]) -> str:
    if not result.get("apply"):
        return "\n".join((
            "MEMORY INIT PREVIEW",
            f"would_queue={int(result.get('would_queue', 0) or 0)}  "
            f"would_deliver={int(result.get('would_deliver', 0) or 0)}  "
            f"already_present={int(result.get('already_present', 0) or 0)}",
            f"excluded={int(result.get('excluded', 0) or 0)}",
            "RUN  ats-lab memory init",
        ))
    state = "READY" if result.get("ready") else "ATTENTION"
    outbox = result.get("outbox") or {}
    return "\n".join((
        f"MEMORY INIT {state}",
        f"queued={int(result.get('queued', 0) or 0)}  "
        f"delivered_now={int(result.get('delivered', 0) or 0)}  "
        f"excluded={int(result.get('excluded', 0) or 0)}",
        f"total_delivered={int(outbox.get('delivered', 0) or 0)}  "
        f"pending={int(outbox.get('pending', 0) or 0)}  "
        f"retry={int(outbox.get('retry', 0) or 0)}",
    ))


def render_memory_sync(result: Mapping[str, Any]) -> str:
    mode = "COMPLETE" if result.get("apply") else "PREVIEW"
    return "\n".join((
        f"MEMORY SYNC {mode}",
        f"eligible={int(result.get('eligible', 0) or 0)}  "
        f"delivered={int(result.get('delivered', 0) or 0)}  "
        f"retry={int(result.get('retry', 0) or 0)}",
        "NEXT  ats-lab memory status",
    ))
