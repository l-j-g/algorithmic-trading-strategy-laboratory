"""Synthesis cohorts, chain leasing, and significance reconciliation."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import utc_now

_MAX_RECENT_SYNTHESIS_FAILURES = 2


class SynthesisMixin:
    def remaining_chain_count(self) -> int:
        """Count unresolved research chains, avoiding significance/baseline double-counting."""
        with self.connect() as connection:
            tracked = connection.execute(
                """SELECT COUNT(*) FROM synthesis_cohort_chains chain
                   WHERE EXISTS (
                       SELECT 1 FROM json_each(chain.work_item_ids_json) member
                       JOIN work_items w ON w.id=member.value
                       WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                         AND COALESCE(
                           json_extract(
                             w.specification_json,'$.readiness.status'
                           ),
                           'ready'
                         )!='requirements_pending'
                   )"""
            ).fetchone()[0]
            untracked = connection.execute(
                """SELECT COUNT(DISTINCT w.experiment_id) FROM work_items w
                   WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                     AND COALESCE(
                       json_extract(
                         w.specification_json,'$.readiness.status'
                       ),
                       'ready'
                     )!='requirements_pending'
                     AND NOT EXISTS (
                         SELECT 1 FROM synthesis_cohort_chains chain,
                              json_each(chain.work_item_ids_json) member
                         WHERE member.value=w.id
                     )"""
            ).fetchone()[0]
            return int(tracked) + int(untracked)

    def reserve_synthesis_cohort(
        self, *, worker_id: str, requested_count: int, low_watermark: int,
        lease_seconds: int, retry_cooldown_seconds: int,
    ) -> dict | None:
        """Acquire the single planner lease when unresolved chains reach the refill watermark."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat().replace("+00:00", "Z")
        lease_expires = (now + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")
        cooldown_start = (now - timedelta(seconds=retry_cooldown_seconds)).isoformat().replace("+00:00", "Z")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """UPDATE synthesis_cohorts SET status='failed',
                   failure_detail=COALESCE(failure_detail, 'planner lease expired'), updated_at=?
                   WHERE status='planning' AND lease_expires_at<=?""",
                (now_text, now_text),
            )
            if connection.execute(
                "SELECT 1 FROM synthesis_cohorts WHERE status='planning' LIMIT 1"
            ).fetchone():
                return None
            remaining = self._remaining_chain_count(connection)
            if remaining > low_watermark:
                return None
            recent_failures = connection.execute(
                """SELECT COUNT(*) FROM synthesis_cohorts
                   WHERE status='failed' AND updated_at>?""",
                (cooldown_start,),
            ).fetchone()[0]
            # A failed planning response is not a blocker for an underfilled
            # queue: permit one replacement attempt. Bound repeated provider
            # failures with the normal retry cooldown after two attempts.
            if int(recent_failures) >= _MAX_RECENT_SYNTHESIS_FAILURES or (
                int(recent_failures) and remaining <= 0
            ):
                return None
            cohort_id = f"COHORT-{uuid.uuid4().hex[:12].upper()}"
            connection.execute(
                """INSERT INTO synthesis_cohorts(
                       id,status,requested_count,remaining_at_trigger,planned_by,
                       lease_expires_at,created_at,updated_at
                   ) VALUES (?, 'planning', ?, ?, ?, ?, ?, ?)""",
                (cohort_id, requested_count, remaining, worker_id, lease_expires, now_text, now_text),
            )
            return {
                "id": cohort_id, "requested_count": requested_count,
                "remaining_at_trigger": remaining, "lease_expires_at": lease_expires,
            }

    @staticmethod
    def _remaining_chain_count(connection: sqlite3.Connection) -> int:
        tracked = connection.execute(
            """SELECT COUNT(*) FROM synthesis_cohort_chains chain
               WHERE EXISTS (
                   SELECT 1 FROM json_each(chain.work_item_ids_json) member
                   JOIN work_items w ON w.id=member.value
                   WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                     AND COALESCE(
                       json_extract(
                         w.specification_json,'$.readiness.status'
                       ),
                       'ready'
                     )!='requirements_pending'
               )"""
        ).fetchone()[0]
        untracked = connection.execute(
            """SELECT COUNT(DISTINCT w.experiment_id) FROM work_items w
               WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                 AND COALESCE(
                   json_extract(
                     w.specification_json,'$.readiness.status'
                   ),
                   'ready'
                 )!='requirements_pending'
                 AND NOT EXISTS (
                     SELECT 1 FROM synthesis_cohort_chains chain,
                          json_each(chain.work_item_ids_json) member
                     WHERE member.value=w.id
                 )"""
        ).fetchone()[0]
        return int(tracked) + int(untracked)

    def activate_synthesis_cohort(self, cohort_id: str, chains: list[dict]) -> None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,requested_count FROM synthesis_cohorts WHERE id=?", (cohort_id,)
            ).fetchone()
            if row is None or row["status"] != "planning":
                raise ValueError(f"cohort is not planning: {cohort_id}")
            if len(chains) != row["requested_count"]:
                raise ValueError(
                    f"cohort {cohort_id} requires {row['requested_count']} chains, got {len(chains)}"
                )
            for chain in chains:
                connection.execute(
                    """INSERT INTO synthesis_cohort_chains(
                           cohort_id,slot,lane,source_experiment_id,work_item_ids_json
                       ) VALUES (?, ?, ?, ?, ?)""",
                    (cohort_id, chain["slot"], chain["lane"], chain.get("source_experiment_id"),
                     json.dumps(chain["work_item_ids"])),
                )
            connection.execute(
                """UPDATE synthesis_cohorts SET status='active', generated_count=?,
                   lease_expires_at=NULL, updated_at=? WHERE id=?""",
                (len(chains), now, cohort_id),
            )

    def fail_synthesis_cohort(self, cohort_id: str, detail: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE synthesis_cohorts SET status='failed', failure_detail=?,
                   lease_expires_at=NULL, updated_at=? WHERE id=? AND status='planning'""",
                (detail, utc_now(), cohort_id),
            )

    def refresh_synthesis_cohorts(self) -> int:
        """Mark cohorts drained when every member chain is terminal."""
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE synthesis_cohorts SET status='drained', updated_at=?
                   WHERE status='active' AND NOT EXISTS (
                       SELECT 1 FROM synthesis_cohort_chains chain
                       WHERE chain.cohort_id=synthesis_cohorts.id AND EXISTS (
                           SELECT 1 FROM json_each(chain.work_item_ids_json) member
                           JOIN work_items w ON w.id=member.value
                           WHERE w.state IN ('scheduled','ready','running','waiting_retry')
                       )
                   )""",
                (utc_now(),),
            )
            return cursor.rowcount

    def synthesis_status(self) -> dict:
        rows = self.rows(
            """SELECT id,status,requested_count,generated_count,remaining_at_trigger,
                      planned_by,lease_expires_at,failure_detail,created_at,updated_at
               FROM synthesis_cohorts ORDER BY created_at DESC LIMIT 1"""
        )
        return {
            "remaining_chains": self.remaining_chain_count(),
            "latest_cohort": rows[0] if rows else None,
        }

    def _binding_cohort_p_value(
        self, connection: sqlite3.Connection, fingerprint: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT r.id, r.experiment_id,
                      json_extract(r.metrics_json, '$.p_value') AS p_value
               FROM runs r JOIN experiments e ON e.id=r.experiment_id
               WHERE e.experiment_type='significance' AND r.status='finished'
                 AND json_extract(e.specification_json, '$.entry_rule.fingerprint')=?
                 AND json_extract(r.metrics_json, '$.p_value') IS NOT NULL
               ORDER BY COALESCE(r.finished_at, r.started_at) ASC, r.id ASC LIMIT 1""",
            (fingerprint,),
        ).fetchone()

    def _release_dependents(
        self, connection: sqlite3.Connection, work_item_id: str, target: str,
        decision: str, active_limit: int, active: int, now: str,
        findings: dict | None = None,
    ) -> tuple[list[str], int]:
        changed: list[str] = []
        dependents = connection.execute(
            """SELECT id,specification_json FROM work_items
               WHERE state='scheduled' AND EXISTS (
                   SELECT 1 FROM json_each(work_items.dependencies_json)
                   WHERE value=?
               ) ORDER BY priority,created_at,id""",
            (work_item_id,),
        ).fetchall()
        for row in dependents:
            state = target
            if target == "ready" and int(active) >= active_limit:
                state = "scheduled"
            elif state == "ready":
                active += 1
            specification = json.loads(row["specification_json"])
            specification["gate_decision"] = (
                decision if state != "scheduled" else "significance_passed_capacity_held"
                if decision == "significance_passed" else decision
            )
            if findings is not None:
                specification["gate_findings"] = findings
            connection.execute(
                """UPDATE work_items SET state=?,specification_json=?,updated_at=?
                   WHERE id=? AND state='scheduled'""",
                (state, json.dumps(specification, sort_keys=True), now, row["id"]),
            )
            changed.append(row["id"])
        return changed, active

    def reconcile_significance_gate(
        self, work_item_id: str, p_value: float, active_limit: int,
        fdr_level: float = 0.05,
    ) -> dict:
        """Release or terminalize baselines dependent on completed significance work.

        First-test-wins: only the earliest finished significance run for an
        entry fingerprint may flip dependent readiness. Later tests are stored
        but reported as superseded without touching dependents. Cohort members
        are additionally gated by Benjamini-Hochberg FDR control across the
        whole cohort family once every member has a binding test.
        """
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute(
                "SELECT experiment_id,specification_json FROM work_items WHERE id=?",
                (work_item_id,),
            ).fetchone()
            fingerprint = None
            cohort_id = None
            if item is not None:
                specification = json.loads(item["specification_json"])
                entry_rule = specification.get("entry_rule")
                if isinstance(entry_rule, dict):
                    fingerprint = entry_rule.get("fingerprint")
                    cohort_id = entry_rule.get("cohort_id")
            if cohort_id:
                from .synthesis import benjamini_hochberg
                members = connection.execute(
                    """SELECT w.id AS work_item_id, w.experiment_id
                       FROM work_items w
                       WHERE json_extract(w.specification_json,'$.operation')='significance'
                         AND json_extract(w.specification_json,'$.entry_rule.cohort_id')=?
                       ORDER BY json_extract(w.specification_json,'$.entry_rule.cohort_slot') ASC, w.id ASC""",
                    (cohort_id,),
                ).fetchall()
                family: list[dict[str, Any]] = []
                for member in members:
                    member_fingerprint = None
                    member_experiment = connection.execute(
                        "SELECT specification_json FROM experiments WHERE id=?",
                        (member["experiment_id"],),
                    ).fetchone()
                    if member_experiment is not None:
                        member_specification = json.loads(
                            member_experiment["specification_json"]
                        )
                        member_entry_rule = member_specification.get("entry_rule")
                        if isinstance(member_entry_rule, dict):
                            member_fingerprint = member_entry_rule.get("fingerprint")
                    binding = (
                        self._binding_cohort_p_value(connection, member_fingerprint)
                        if member_fingerprint else None
                    )
                    family.append({
                        "work_item_id": str(member["work_item_id"]),
                        "p_value": (
                            float(binding["p_value"]) if binding is not None else None
                        ),
                    })
                if any(member["p_value"] is None for member in family):
                    return {
                        "decision": "awaiting_cohort_fdr",
                        "dependents": [],
                        "cohort_fdr": {
                            "cohort_id": cohort_id, "fdr_level": fdr_level,
                            "family_size": len(family),
                            "tested": sum(
                                member["p_value"] is not None for member in family
                            ),
                        },
                    }
                findings = benjamini_hochberg(
                    [member["p_value"] for member in family], fdr_level,
                )
                changed: list[str] = []
                active = connection.execute(
                    "SELECT COUNT(*) FROM work_items WHERE state IN ('ready','running')"
                ).fetchone()[0]
                member_findings: list[dict[str, Any]] = []
                for member, finding in zip(family, findings):
                    raw = member["p_value"]
                    if finding["rejected"] and raw < 0.05:
                        target, decision = "ready", "significance_passed_bh_fdr"
                    elif not finding["rejected"] and raw < 0.05:
                        target, decision = "scheduled", "significance_withheld_bh_fdr"
                    elif raw <= 0.10:
                        target, decision = "scheduled", "significance_inconclusive"
                    else:
                        target, decision = "archived", "significance_failed"
                    gate_findings = {
                        "procedure": "benjamini_hochberg",
                        "fdr_level": fdr_level,
                        "family_size": len(family),
                        "rank": finding["rank"],
                        "threshold": finding["threshold"],
                        "rejected": finding["rejected"],
                    }
                    released, active = self._release_dependents(
                        connection, member["work_item_id"], target, decision,
                        active_limit, active, now, findings=gate_findings,
                    )
                    changed.extend(released)
                    member_findings.append({
                        **member, **finding, "decision": decision,
                    })
                return {
                    "decision": "cohort_fdr_applied",
                    "dependents": changed,
                    "cohort_fdr": {
                        "cohort_id": cohort_id, "fdr_level": fdr_level,
                        "family_size": len(family), "members": member_findings,
                    },
                }
            binding = None
            if fingerprint:
                binding = self._binding_cohort_p_value(connection, fingerprint)
            if binding is not None and item is not None \
                    and binding["experiment_id"] != item["experiment_id"]:
                return {"decision": "superseded_by_first_test", "dependents": []}
            if binding is not None:
                p_value = float(binding["p_value"])
            if p_value < 0.05:
                target, decision = "ready", "significance_passed"
            elif p_value <= 0.10:
                target, decision = "scheduled", "significance_inconclusive"
            else:
                target, decision = "archived", "significance_failed"
            active = connection.execute(
                "SELECT COUNT(*) FROM work_items WHERE state IN ('ready','running')"
            ).fetchone()[0]
            changed, _ = self._release_dependents(
                connection, work_item_id, target, decision, active_limit, active, now,
            )
        return {"decision": decision, "dependents": changed}
