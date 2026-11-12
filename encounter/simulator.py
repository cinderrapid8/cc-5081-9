"""Simulation harness: test characters and a scripted action queue.

The harness owns no encounter logic.  Each action is a small declarative step
that calls the coordinator and then pumps the virtual clock, so the same
scripts drive the live rehearsal and the edge-case tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List

from .clock import Clock
from .coordinator import PhaseCoordinator
from .events import EventBus
from .recorder import RunRecorder
from .scenario import BOSS_ID, build_encounter_config


@dataclass
class Actor:
    actor_id: str
    role: str

    def strike(self, coordinator: PhaseCoordinator, target_id: str, amount: float) -> None:
        coordinator.apply_damage(target_id, amount, source=self.actor_id)

    def break_mechanic(self, coordinator: PhaseCoordinator, mechanic_id: str) -> bool:
        return coordinator.interrupt_mechanic(mechanic_id, self.actor_id)

    def fall(self, coordinator: PhaseCoordinator) -> None:
        coordinator.player_dies(self.actor_id)


class Simulator:
    def __init__(self) -> None:
        self.bus = EventBus()
        self.clock = Clock()
        self.config = build_encounter_config()
        self.coordinator = PhaseCoordinator(self.config, self.bus, self.clock)
        self.recorder = RunRecorder(self.config.encounter_id)
        self.bus.subscribe(self.recorder)
        self.actors = [
            Actor("player-1", "tank"),
            Actor("player-2", "damage"),
            Actor("player-3", "support"),
        ]
        self._log: List[str] = []

    def actor(self, actor_id: str) -> Actor:
        return next(a for a in self.actors if a.actor_id == actor_id)

    def run(self, actions: List[Callable[["Simulator"], Any]]) -> None:
        for action in actions:
            result = action(self)
            self._log.append(f"{action.__name__ if hasattr(action, '__name__') else 'step'} -> {result}")

    # -- scripted step factories ----------------------------------------- #

    def start(self) -> Callable[["Simulator"], Any]:
        def step(sim: Simulator) -> str:
            sim.coordinator.start_encounter()
            return "started"

        step.__name__ = "start_encounter"
        return step

    def wait(self, seconds: float) -> Callable[["Simulator"], Any]:
        def step(sim: Simulator) -> str:
            sim.coordinator.tick(sim.clock.now + seconds)
            return f"waited {seconds}s (t={sim.clock.now})"

        step.__name__ = f"wait_{seconds}"
        return step

    def damage(self, actor_id: str, target_id: str, amount: float) -> Callable[["Simulator"], Any]:
        def step(sim: Simulator) -> str:
            sim.actor(actor_id).strike(sim.coordinator, target_id, amount)
            return f"{actor_id} hit {target_id} for {amount}"

        step.__name__ = f"damage_{target_id}_{amount}"
        return step

    def boss_damage(self, amount: float, actor_id: str = "player-2") -> Callable[["Simulator"], Any]:
        return self.damage(actor_id, BOSS_ID, amount)

    def destroy(self, actor_id: str, target_id: str) -> Callable[["Simulator"], Any]:
        def step(sim: Simulator) -> str:
            sim.coordinator.destroy_target(target_id, source=actor_id)
            return f"{actor_id} destroyed {target_id}"

        step.__name__ = f"destroy_{target_id}"
        return step

    def interrupt(self, actor_id: str, mechanic_id: str) -> Callable[["Simulator"], Any]:
        def step(sim: Simulator) -> str:
            ok = sim.actor(actor_id).break_mechanic(sim.coordinator, mechanic_id)
            return f"interrupt {mechanic_id} -> {ok}"

        step.__name__ = f"interrupt_{mechanic_id}"
        return step

    def kill_player(self, actor_id: str) -> Callable[["Simulator"], Any]:
        def step(sim: Simulator) -> str:
            sim.actor(actor_id).fall(sim.coordinator)
            return f"{actor_id} down"

        step.__name__ = f"kill_{actor_id}"
        return step

    def revive(self, actor_id: str) -> Callable[["Simulator"], Any]:
        def step(sim: Simulator) -> str:
            sim.coordinator.player_revived(actor_id)
            return f"{actor_id} revived"

        step.__name__ = f"revive_{actor_id}"
        return step

    def reset(self) -> Callable[["Simulator"], Any]:
        def step(sim: Simulator) -> str:
            sim.coordinator.reset_after_wipe()
            return "reset"

        step.__name__ = "reset_after_wipe"
        return step
