"""Runtime state of a mechanic instance.

A *committed* mechanic has passed eligibility and acquired its claims.  Its
lifecycle is explicit; nothing ends implicitly because an animation finished.
Pending (scheduled but not yet committed) mechanics live only as timer entries
and are revoked by the coordinator when their window closes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .config import MechanicConfig


class MechanicStatus(str, Enum):
    COMMITTED = "committed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    REVOKED = "revoked"


class RevokeReason(str, Enum):
    PHASE_CHANGE = "phase_change"
    WIPE = "wipe"
    ENCOUNTER_END = "encounter_end"
    RESOURCE_BUSY = "resource_busy"
    TARGET_GONE = "target_gone"
    INTERRUPTED = "interrupted"


@dataclass
class MechanicInstance:
    config: MechanicConfig
    status: MechanicStatus
    start_time: float
    end_time: Optional[float] = None
    end_timer_id: Optional[int] = None
    generation: int = 0
    detail: Optional[str] = None

    @property
    def mechanic_id(self) -> str:
        return self.config.mechanic_id

    @property
    def active(self) -> bool:
        return self.status is MechanicStatus.COMMITTED
