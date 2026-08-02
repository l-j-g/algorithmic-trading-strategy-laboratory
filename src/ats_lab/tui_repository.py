"""Read-only data source for reusable ATS Lab terminal views."""
from __future__ import annotations

from typing import Any, Protocol

from .cli_ux import next_guidance
from .console import distinct_candidate_evidence, monitor_snapshot
from .database import WorkflowDatabase
from .research_memory import memory_status
from .tui_types import CANDIDATE_VERDICT_ORDER, QUEUE_STATE_ORDER


class TuiDataSource(Protocol):
    def load(self) -> dict[str, Any]: ...


class TuiRepository:
    """Build bounded UI projections without exposing raw evidence or source."""

    def __init__(self, database: WorkflowDatabase) -> None:
        self.database = database

    def load(self) -> dict[str, Any]:
        snapshot = monitor_snapshot(self.database)
        queue_order = " ".join(
            f"WHEN '{state.value}' THEN {index}"
            for index, state in enumerate(QUEUE_STATE_ORDER)
        )
        queue = self.database.rows(
            f"""SELECT q.id,q.experiment_id,q.strategy,q.priority,q.state,
                      q.attempts,q.retry_after,q.blocker_code,q.blocker_detail,
                      q.created_at,e.experiment_type,e.archetype
               FROM active_queue q JOIN experiments e ON e.id=q.experiment_id
               ORDER BY CASE q.state {queue_order} ELSE 99 END,
                        q.priority,q.created_at,q.id LIMIT 500"""
        )
        all_evidence = self.database.query_normalized_evidence(limit=5000)
        candidates = [
            item.to_dict()
            for item in distinct_candidate_evidence(all_evidence)
        ]
        verdict_rank = {
            verdict.value: index
            for index, verdict in enumerate(CANDIDATE_VERDICT_ORDER)
        }
        candidates.sort(key=lambda row: (
            verdict_rank.get(row.get("verdict"), len(verdict_rank)),
            str(row.get("strategy") or ""),
        ))
        hpo_query = getattr(self.database, "hpo_studies", None)
        hpo = hpo_query(limit=500) if hpo_query else []
        memories = self.database.rows(
            """SELECT state,attempts,created_at,delivered_at,
                      json_extract(payload_json,'$.strategy') AS strategy,
                      json_extract(payload_json,'$.lifecycle_stage') AS lifecycle_stage,
                      json_extract(payload_json,'$.verdict') AS verdict
               FROM research_memory_outbox ORDER BY id DESC LIMIT 500"""
        )
        memory = memory_status(self.database)
        latest_evidence: dict[str, dict[str, Any]] = {}
        for evidence in all_evidence:
            latest_evidence.setdefault(
                evidence.experiment_id, evidence.to_dict(),
            )
        columns = []
        for queue_item in queue:
            evidence = latest_evidence.get(queue_item["experiment_id"], {})
            columns.append({
                **queue_item,
                "item": queue_item["id"],
                "symbol": evidence.get("symbol"),
                "timeframe": evidence.get("timeframe"),
                "verdict": evidence.get("verdict"),
                "net_profit_percentage": evidence.get("net_profit_percentage"),
                "sharpe_ratio": evidence.get("sharpe_ratio"),
                "trade_count": evidence.get("trade_count"),
                "next": (
                    queue_item.get("blocker_detail")
                    or evidence.get("next_action")
                    or queue_item.get("blocker_code")
                ),
            })
        return {
            "snapshot": snapshot,
            "queue": queue,
            "candidates": candidates,
            "hpo": hpo,
            "memories": memories,
            "columns": columns,
            "memory": memory,
            "guidance": next_guidance(snapshot, memory),
        }


def build_tui_model(database: WorkflowDatabase) -> dict[str, Any]:
    """Compatibility helper around the reusable repository abstraction."""
    return TuiRepository(database).load()
