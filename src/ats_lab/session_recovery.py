"""Bounded Jesse session lifecycle recovery policy."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionRecoveryPolicy:
    """Allow one reconciliation before routing session evidence to analysis."""

    max_recovery_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_recovery_attempts != 1:
            raise ValueError("session recovery allows exactly one attempt")

    def exhausted(
        self,
        state: str,
        *,
        recovery_attempted: bool = False,
    ) -> bool:
        """Return whether another execution retry would be unbounded noise."""
        if state in {"start_recovery_failed", "zombie_recovery_required"}:
            return True
        return state == "malformed_session" and recovery_attempted
