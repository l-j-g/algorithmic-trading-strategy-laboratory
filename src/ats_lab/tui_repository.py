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
            f"""SELECT id,strategy,priority,state,attempts,retry_after,
                      blocker_code,blocker_detail,created_at
               FROM active_queue
               ORDER BY CASE state {queue_order} ELSE 99 END,
                        priority,created_at,id LIMIT 500"""
        )
        candidates = [
            item.to_dict() for item in distinct_candidate_evidence(
                self.database.query_normalized_evidence(limit=5000)
            )
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
        return {
            "snapshot": snapshot,
            "queue": queue,
            "candidates": candidates,
            "hpo": hpo,
            "memories": memories,
            "memory": memory,
            "guidance": next_guidance(snapshot, memory),
        }


def build_tui_model(database: WorkflowDatabase) -> dict[str, Any]:
    """Compatibility helper around the reusable repository abstraction."""
    return TuiRepository(database).load()
