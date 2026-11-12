"""Configuration model for an encounter.

Everything that distinguishes one encounter from another lives here: bosses,
destructible targets, phases and the mechanics each phase may launch.  Configs
can be built programmatically or loaded from JSON (see :class:`ConfigLoader`),
so mechanics are data rather than hard-coded behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TriggerType(str, Enum):
    HP_THRESHOLD = "hp_threshold"
    TIMER = "timer"


class CrossPhasePolicy(str, Enum):
    """What happens to an already-committed mechanic when its phase ends."""

    CONTINUE = "continue"
    CANCEL = "cancel"


class ConditionType(str, Enum):
    HP_BELOW = "hp_below"
    TARGET_STATE = "target_state"
    ELAPSED = "elapsed"


class TargetState(str, Enum):
    ALIVE = "alive"
    DESTROYED = "destroyed"


@dataclass(frozen=True)
class TargetConfig:
    target_id: str
    name: str
    max_hp: float
    optional: bool = False


@dataclass(frozen=True)
class MechanicConfig:
    mechanic_id: str
    name: str
    trigger: TriggerType
    # HP percentage (0..100) for hp_threshold, or absolute time for timer.
    value: float
    duration: float = 0.0
    # Object/position resources, e.g. ("object:artifact", "position:north").
    resources: List[str] = field(default_factory=list)
    mutex_group: Optional[str] = None
    cross_phase: CrossPhasePolicy = CrossPhasePolicy.CANCEL
    # Target ids that must still be alive when the mechanic becomes eligible.
    required_targets: List[str] = field(default_factory=list)
    # Timer mechanics only: re-arm every N seconds while the phase is active.
    repeat_interval: Optional[float] = None
    # Lower number wins when several mechanics are eligible at the same instant.
    priority: int = 100
    interruptible: bool = False

    @property
    def is_periodic(self) -> bool:
        return self.trigger is TriggerType.TIMER and self.repeat_interval is not None


@dataclass(frozen=True)
class PhaseCondition:
    kind: ConditionType
    # hp_below: percentage; elapsed: seconds; target_state: the target id.
    ref: Any
    state: Optional[TargetState] = None


@dataclass(frozen=True)
class PhaseConfig:
    phase_id: str
    name: str
    entry: List[PhaseCondition]
    mechanics: List[MechanicConfig]
    # Entering this phase wins the encounter immediately (the final burn phase).
    victory_phase: bool = False


@dataclass(frozen=True)
class BossConfig:
    boss_id: str
    name: str
    max_hp: float


@dataclass(frozen=True)
class EncounterConfig:
    encounter_id: str
    name: str
    boss: BossConfig
    targets: List[TargetConfig]
    phases: List[PhaseConfig]
    # Wipe when every one of these player slots is dead.
    player_slots: int = 3

    def mechanic(self, mechanic_id: str) -> MechanicConfig:
        for phase in self.phases:
            for mechanic in phase.mechanics:
                if mechanic.mechanic_id == mechanic_id:
                    return mechanic
        raise KeyError(f"unknown mechanic {mechanic_id!r}")

    def phase_of(self, mechanic_id: str) -> PhaseConfig:
        for phase in self.phases:
            if any(m.mechanic_id == mechanic_id for m in phase.mechanics):
                return phase
        raise KeyError(f"unknown mechanic {mechanic_id!r}")

    def validate(self) -> None:
        if not self.phases:
            raise ValueError("encounter needs at least one phase")
        if self.phases[0].entry:
            raise ValueError("the first phase must have no entry conditions")
        target_ids = {t.target_id for t in self.targets}
        known_mechanics: set[str] = set()
        for phase in self.phases:
            for mechanic in phase.mechanics:
                if mechanic.mechanic_id in known_mechanics:
                    raise ValueError(f"duplicate mechanic id {mechanic.mechanic_id!r}")
                known_mechanics.add(mechanic.mechanic_id)
                missing = set(mechanic.required_targets) - target_ids
                if missing:
                    raise ValueError(
                        f"mechanic {mechanic.mechanic_id!r} requires unknown targets {sorted(missing)}"
                    )
                if mechanic.trigger is TriggerType.HP_THRESHOLD and not 0 <= mechanic.value <= 100:
                    raise ValueError(
                        f"mechanic {mechanic.mechanic_id!r} hp threshold must be a percentage"
                    )
                if mechanic.trigger is TriggerType.TIMER and mechanic.value < 0:
                    raise ValueError(f"mechanic {mechanic.mechanic_id!r} timer cannot be negative")
        for phase in self.phases[1:]:
            for condition in phase.entry:
                if condition.kind is ConditionType.TARGET_STATE and condition.ref not in target_ids:
                    raise ValueError(
                        f"phase {phase.phase_id!r} gates on unknown target {condition.ref!r}"
                    )


class ConfigLoader:
    """Builds an :class:`EncounterConfig` from plain JSON-friendly dictionaries."""

    @staticmethod
    def load(data: Dict[str, Any]) -> EncounterConfig:
        boss = BossConfig(**data["boss"])
        targets = [TargetConfig(**t) for t in data.get("targets", [])]
        phases = [ConfigLoader._phase(p) for p in data["phases"]]
        config = EncounterConfig(
            encounter_id=data["encounter_id"],
            name=data["name"],
            boss=boss,
            targets=targets,
            phases=phases,
            player_slots=data.get("player_slots", 3),
        )
        config.validate()
        return config

    @staticmethod
    def load_file(path: str) -> EncounterConfig:
        with open(path, "r", encoding="utf-8") as handle:
            return ConfigLoader.load(json.load(handle))

    @staticmethod
    def _phase(data: Dict[str, Any]) -> PhaseConfig:
        conditions = []
        for condition in data.get("entry", []):
            kind = ConditionType(condition["kind"])
            state = TargetState(condition["state"]) if "state" in condition else None
            conditions.append(PhaseCondition(kind=kind, ref=condition["ref"], state=state))
        mechanics = [ConfigLoader._mechanic(m) for m in data.get("mechanics", [])]
        return PhaseConfig(
            phase_id=data["phase_id"],
            name=data["name"],
            entry=conditions,
            mechanics=mechanics,
            victory_phase=data.get("victory_phase", False),
        )

    @staticmethod
    def _mechanic(data: Dict[str, Any]) -> MechanicConfig:
        return MechanicConfig(
            mechanic_id=data["mechanic_id"],
            name=data["name"],
            trigger=TriggerType(data["trigger"]),
            value=data["value"],
            duration=data.get("duration", 0.0),
            resources=list(data.get("resources", [])),
            mutex_group=data.get("mutex_group"),
            cross_phase=CrossPhasePolicy(data.get("cross_phase", "cancel")),
            required_targets=list(data.get("required_targets", [])),
            repeat_interval=data.get("repeat_interval"),
            priority=data.get("priority", 100),
            interruptible=data.get("interruptible", False),
        )
