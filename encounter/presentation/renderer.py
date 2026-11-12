"""Presentation layer driven exclusively by coordinator events.

The renderer keeps a view model that it mutates only inside its event handler.
It has no reference to the coordinator and no timers of its own: there is no
code path by which an animation "finishing" could advance a phase.  A text
transcript is produced for the rehearsal, but the same view model could feed a
real game client unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..events import Event


@dataclass
class MechanicView:
    mechanic_id: str
    name: str
    phase_id: str
    start_time: float
    resources: List[str]
    cross_phase: str
    status: str = "committed"


@dataclass
class ViewModel:
    status: str = "idle"
    generation: int = 0
    phase: Optional[str] = None
    boss_hp: Optional[float] = None
    boss_max: Optional[float] = None
    targets: Dict[str, float] = field(default_factory=dict)
    players: List[str] = field(default_factory=list)
    active_mechanics: Dict[str, MechanicView] = field(default_factory=dict)
    claims: Dict[str, str] = field(default_factory=dict)


class EventRenderer:
    """Builds a transcript and a live view model from the event stream."""

    def __init__(self) -> None:
        self.view = ViewModel()
        self.transcript: List[str] = []
        self._unrecognized: List[str] = []

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is None:
            self._unrecognized.append(event.type)
            self.transcript.append(f"t={event.time:>6.1f}  (unhandled {event.type})")
            return
        line = handler(event)
        if line:
            self.transcript.append(f"t={event.time:>6.1f}  {line}")

    @property
    def unrecognized_events(self) -> List[str]:
        return list(self._unrecognized)

    # -- encounter / phase ------------------------------------------------ #

    def _on_encounter_start(self, event: Event) -> str:
        p = event.payload
        self.view.status = "active"
        self.view.generation = p["generation"]
        self.view.players = list(p["players"])
        return f"ENCOUNTER START  gen={p['generation']} players={p['players']}"

    def _on_encounter_reset(self, event: Event) -> str:
        self.view = ViewModel(status="idle", generation=event.payload["generation"])
        return "RESET -> idle, view cleared"

    def _on_phase_enter(self, event: Event) -> str:
        p = event.payload
        self.view.phase = p["phase_id"]
        return f"PHASE {p['index'] + 1} ENTER  {p['phase_id']} ({p['phase_name']})"

    def _on_victory(self, event: Event) -> str:
        self.view.status = "victory"
        return "VICTORY"

    def _on_wipe(self, event: Event) -> str:
        self.view.status = "wipe"
        return "WIPE"

    def _on_round_clean(self, event: Event) -> str:
        p = event.payload
        self.view.active_mechanics.clear()
        self.view.claims.clear()
        return f"ROUND CLEAN  residual claims={p['claims']}"

    # -- combat ----------------------------------------------------------- #

    def _on_damage(self, event: Event) -> str:
        p = event.payload
        if p["target"].startswith("boss"):
            self.view.boss_hp = p["hp_after"]
        else:
            self.view.targets[p["target"]] = p["hp_after"]
        return (
            f"DAMAGE {p['target']} -{p['amount']:.0f} "
            f"({p['hp_before']:.0f} -> {p['hp_after']:.0f}) by {p['source']}"
        )

    def _on_target_destroyed(self, event: Event) -> str:
        self.view.targets[event.payload["target"]] = 0.0
        return f"TARGET DESTROYED {event.payload['target']}"

    def _on_player_down(self, event: Event) -> str:
        p = event.payload
        self.view.players = list(p["alive"])
        return f"PLAYER DOWN {p['player']}  alive={p['alive']}"

    def _on_player_revived(self, event: Event) -> str:
        p = event.payload
        self.view.players = list(p["alive"])
        return f"PLAYER REVIVED {p['player']}  alive={p['alive']}"

    # -- mechanics -------------------------------------------------------- #

    def _on_mechanic_start(self, event: Event) -> str:
        p = event.payload
        self.view.active_mechanics[p["mechanic_id"]] = MechanicView(
            mechanic_id=p["mechanic_id"],
            name=p["name"],
            phase_id=p["phase_id"],
            start_time=event.time,
            resources=list(p["resources"]),
            cross_phase=p["cross_phase"],
        )
        for resource in p["resources"]:
            self.view.claims[resource] = p["mechanic_id"]
        if p["mutex_group"]:
            self.view.claims[f"group:{p['mutex_group']}"] = p["mechanic_id"]
        return (
            f"MECHANIC START {p['mechanic_id']} [{p['trigger']}] "
            f"resources={p['resources']} mutex={p['mutex_group']} "
            f"cross_phase={p['cross_phase']}"
        )

    def _on_mechanic_end(self, event: Event) -> str:
        p = event.payload
        self.view.active_mechanics.pop(p["mechanic_id"], None)
        for resource in p["resources"]:
            self.view.claims.pop(resource, None)
        return (
            f"MECHANIC END {p['mechanic_id']} outcome={p['outcome']} "
            f"reason={p['reason']}"
            + (f" actor={p['actor']}" if p.get("actor") else "")
        )

    def _on_mechanic_blocked(self, event: Event) -> str:
        p = event.payload
        return (
            f"MECHANIC BLOCKED {p['mechanic_id']} reason={p['reason']} "
            f"resource={p.get('resource')} held_by={p.get('holder')}"
        )

    def _on_mechanic_revoked(self, event: Event) -> str:
        p = event.payload
        return f"MECHANIC REVOKED {p['mechanic_id']} reason={p['reason']}"

    def _on_mechanic_deferred(self, event: Event) -> str:
        p = event.payload
        return f"MECHANIC DEFERRED {p['mechanic_id']} ({p['reason']})"

    def _on_mechanic_carried_over(self, event: Event) -> str:
        p = event.payload
        return f"MECHANIC CARRIED OVER {p['mechanic_id']} from {p['from_phase']}"

    def _on_interrupt_rejected(self, event: Event) -> str:
        p = event.payload
        return f"INTERRUPT REJECTED {p['mechanic_id']} ({p['reason']})"

    def _on_timer_dropped_stale(self, event: Event) -> str:
        p = event.payload
        return (
            f"STALE TIMER DROPPED id={p['timer_id']} gen={p['timer_generation']} "
            f"!= {p['current_generation']} key={p['key']}"
        )

    def _on_timer_cancelled(self, event: Event) -> str:
        p = event.payload
        return f"TIMER CANCELLED id={p['timer_id']} reason={p['reason']} key={p['key']}"

    def render(self) -> str:
        return "\n".join(self.transcript)
