"""Classify workflow-v1 files for retain, replace, review, or removal."""
from __future__ import annotations

from pathlib import Path


REPLACE = {
    "research/automation/job_state.json": "canonical SQLite",
    "research/automation/headless_runs/*.json": "canonical evidence rows",
    "candidate_dashboard.html": "web dashboard + /candidates",
    "LAST_SESSION_HANDOFF.md": "events table",
    "PROGRESS_LOG.md": "events table",
}
REMOVE_PATTERNS = (
    "research/automation/run_next_job.sh",
    "research/automation/run_research_loop.sh",
    "research/automation/run_until_hpo_candidate.sh",
    "research/automation/analyze_jobs.sh",
    "research/automation/queue_lifecycle.py",
    "research/automation/promotion_scheduler.py",
    "research/automation/research_review.py",
    "research/automation/candidate_dashboard.py",
    "research/archive/automation_prompts/",
)
RETAIN = {
    "AGENTS.md": "Jesse MCP safety boundary; shorten after generated rules are separated",
    "research/BACKTEST_EVALUATION_PROTOCOL.md": "compatibility pointer to ATS-owned evaluation gates",
    "research/STRATEGY_CONCEPT_PLAYBOOK.md": "compatibility pointer to ATS-owned concept library",
}


def build_inventory(repo: Path) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {"retain": [], "replace": [], "remove": [], "review": []}
    candidates: set[Path] = set()
    for base in (repo / "research", repo / "docs", repo / "prompts", repo / "skills", repo / "algorithmic-trading-strategy-laboratory"):
        if base.exists():
            candidates.update(
                path for path in base.rglob("*")
                if path.is_file()
                and ".git" not in path.parts
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal"}
            )
    for path in sorted(candidates):
        relative = str(path.relative_to(repo))
        item = {"path": relative, "bytes": path.stat().st_size}
        if relative in RETAIN:
            item["reason"] = RETAIN[relative]
            result["retain"].append(item)
        elif relative in REPLACE:
            item["replacement"] = REPLACE[relative]
            result["replace"].append(item)
        elif any(relative == pattern or relative.startswith(pattern) for pattern in REMOVE_PATTERNS):
            item["reason"] = "superseded workflow-v1 implementation or archived prompt"
            result["remove"].append(item)
        elif relative.startswith("algorithmic-trading-strategy-laboratory/"):
            item["reason"] = "workflow-v2 implementation"
            result["retain"].append(item)
        else:
            result["review"].append(item)
    return result


def render_markdown(inventory: dict[str, list[dict[str, object]]]) -> str:
    lines = ["# Legacy Workflow Inventory", "", "Generated classification only. No deletion authorization.", ""]
    for category in ("retain", "replace", "remove", "review"):
        items = inventory[category]
        lines.extend((f"## {category.title()} ({len(items)})", ""))
        for item in items:
            detail = item.get("reason") or item.get("replacement") or "manual classification required"
            lines.append(f"- `{item['path']}` ({item['bytes']} bytes) — {detail}")
        lines.append("")
    return "\n".join(lines)
