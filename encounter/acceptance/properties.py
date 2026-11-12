"""The three checkable properties and failure classification.

The acceptance suite never reduces a run to win/lose text.  Every divergence is
attributed to one of the named properties so failures point at the guarantee
that broke:

* ``phase``      -- phase eligibility (entry conditions, in-phase dispatch);
* ``exclusivity`` -- mechanic exclusivity (resource/mutex occupancy);
* ``reset``      -- reset cleanup (claims, mechanics, stale generation timers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

PHASE = "phase"
EXCLUSIVITY = "exclusivity"
RESET = "reset"

PROPERTIES = (PHASE, EXCLUSIVITY, RESET)

PROPERTY_DESCRIPTIONS = {
    PHASE: "A phase is entered only when all entry conditions hold, and no "
    "mechanic dispatches outside its owning phase.",
    EXCLUSIVITY: "Two committed mechanics never share a resource or mutex "
    "group; contention is blocked, never queued.",
    RESET: "A wipe/victory leaves no claim, active mechanic or live timer; a "
    "callback from a dead generation is rejected, not executed.",
}

# Snapshot/counter fields that belong to each property when a diff is reported.
PHASE_FIELDS = frozenset(
    {"phase_id", "phase_index", "status", "boss_hp", "target_hp", "players"}
)
EXCLUSIVITY_FIELDS = frozenset({"occupancy", "active_mechanics"})
RESET_COUNTERS = frozenset({"timer_dropped_stale", "timer_cancelled"})
DISPATCH_COUNTERS = frozenset(
    {
        "mechanic_start",
        "mechanic_end",
        "mechanic_blocked",
        "mechanic_revoked",
        "mechanic_deferred",
        "interrupt_rejected",
    }
)


@dataclass
class PropertyFailure:
    property: str
    scenario_id: str
    step: int
    detail: str
    evidence: Dict[str, Any] = field(default_factory=dict)


def classify_step_failure(
    scenario_id: str, step_index: int, step: Any
) -> List[PropertyFailure]:
    """Map a step's field/counter diffs onto the named properties."""

    failures: List[PropertyFailure] = []
    phase_fields = [d for d in step.snapshot_diffs if d["field"] in PHASE_FIELDS]
    if phase_fields:
        failures.append(
            PropertyFailure(
                PHASE,
                scenario_id,
                step_index,
                "phase trajectory or eligibility state diverged",
                {"diffs": phase_fields},
            )
        )
    exclusive_fields = [
        d for d in step.snapshot_diffs if d["field"] in EXCLUSIVITY_FIELDS
    ]
    dispatch_diffs = [
        d for d in step.counter_diffs if d["counter"] in DISPATCH_COUNTERS
    ]
    if exclusive_fields or dispatch_diffs:
        failures.append(
            PropertyFailure(
                EXCLUSIVITY,
                scenario_id,
                step_index,
                "occupancy set or dispatch counts diverged",
                {"snapshot": exclusive_fields, "counters": dispatch_diffs},
            )
        )
    reset_diffs = [d for d in step.counter_diffs if d["counter"] in RESET_COUNTERS]
    if reset_diffs:
        failures.append(
            PropertyFailure(
                RESET,
                scenario_id,
                step_index,
                "stale-timer or cancellation accounting diverged",
                {"counters": reset_diffs},
            )
        )
    return failures
