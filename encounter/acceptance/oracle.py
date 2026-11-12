"""Independent reference model for the encounter coordinator.

This module deliberately re-implements the published coordinator semantics
from the configuration model alone; it imports neither
:class:`~encounter.coordinator.PhaseCoordinator` nor the resource manager.  Its
sole purpose is to be a second, independently written authority: when the real
coordinator and this model agree on every step of an enumerated sequence, the
properties are backed by two implementations rather than one self-assertion.

The model is intentionally small and explicit.  It mirrors the three property
areas the acceptance suite exists to defend:

* phase eligibility -- entry conditions and threshold ownership are re-derived
  from :class:`~encounter.config.EncounterConfig` on every transition;
* mechanic exclusivity -- claims live in one local dict and every commit goes
  through an atomic check, so overlap is structurally impossible;
* reset cleanup -- wipe/victory clear timers, mechanics and claims together,
  and every timer keeps its generation so a late callback can only be dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..config import (
    ConditionType,
    CrossPhasePolicy,
    EncounterConfig,
    MechanicConfig,
    PhaseConfig,
    TargetState,
    TriggerType,
)
from .actions import Action
from .traces import bump, empty_counters, normalize_snapshot


class OracleError(RuntimeError):
    pass


@dataclass
class _Timer:
    timer_id: int
    due: float
    generation: int
    key: Tuple[Any, ...]
    kind: str  # "phase" | "end"
    mechanic_id: Optional[str]
    periodic: bool = False
    interval: float = 0.0


@dataclass
class _Mechanic:
    mechanic_id: str
    status: str  # committed | completed | cancelled | blocked | revoked
    start_time: float
    end_time: Optional[float] = None
    detail: Optional[str] = None
    end_timer_id: Optional[int] = None

    @property
    def active(self) -> bool:
        return self.status == "committed"


class ReferenceCoordinator:
    """A from-scratch state machine matching the shipped coordinator contract."""

    def __init__(self, config: EncounterConfig) -> None:
        config.validate()
        self.config = config
        self.generation = 0
        self.status = "idle"
        self.phase_index = -1
        self.boss_hp = config.boss.max_hp
        self.target_hp: Dict[str, float] = {t.target_id: t.max_hp for t in config.targets}
        self.players: set[str] = set()
        self.mechanics: Dict[str, _Mechanic] = {}
        self._fired: set[str] = set()
        self._timers: List[_Timer] = []
        self._next_timer_id = 1
        self.now = 0.0
        # resource/group -> owner
        self._claims: Dict[str, str] = {}
        self.phase_trajectory: List[Dict[str, Any]] = []
        self.counters: Dict[str, Any] = empty_counters()

    # ------------------------------------------------------------------ #
    # Observation surface (same shape as the real runner's snapshot)
    # ------------------------------------------------------------------ #

    def snapshot(self) -> Dict[str, Any]:
        return normalize_snapshot(
            {
                "time": self.now,
                "generation": self.generation,
                "status": self.status,
                "phase_id": self._phase_id(self.phase_index),
                "phase_index": self.phase_index,
                "boss_hp": self.boss_hp,
                "target_hp": dict(self.target_hp),
                "players": set(self.players),
                "occupancy": [(resource, owner) for resource, owner in self._claims.items()],
                "active_mechanics": [m.mechanic_id for m in self.mechanics.values() if m.active],
            }
        )

    def _phase_id(self, index: int) -> Optional[str]:
        if 0 <= index < len(self.config.phases):
            return self.config.phases[index].phase_id
        return None

    @property
    def current_phase(self) -> Optional[PhaseConfig]:
        if 0 <= self.phase_index < len(self.config.phases):
            return self.config.phases[self.phase_index]
        return None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start_encounter(self) -> None:
        if self.status == "active":
            raise OracleError("encounter already active")
        if self.generation > 0:
            assert not self._claims, "reset with lingering resource claims"
            assert not [m for m in self.mechanics.values() if m.active]
        self.generation += 1
        self.status = "active"
        self.boss_hp = self.config.boss.max_hp
        self.target_hp = {t.target_id: t.max_hp for t in self.config.targets}
        self.players = {f"player-{i + 1}" for i in range(self.config.player_slots)}
        self.mechanics = {}
        self._fired = set()
        self.phase_index = -1
        self.phase_trajectory = []
        self._enter_phase(0)

    # ------------------------------------------------------------------ #
    # Timers: a tiny independently written scheduler
    # ------------------------------------------------------------------ #

    def _schedule(
        self,
        delay: float,
        key: Tuple[Any, ...],
        kind: str,
        mechanic_id: Optional[str],
        periodic: bool = False,
        interval: float = 0.0,
    ) -> _Timer:
        timer = _Timer(
            timer_id=self._next_timer_id,
            due=self.now + max(0.0, delay),
            generation=self.generation,
            key=key,
            kind=kind,
            mechanic_id=mechanic_id,
            periodic=periodic,
            interval=interval,
        )
        self._next_timer_id += 1
        self._timers.append(timer)
        return timer

    def _pop_due(self, until: float) -> List[_Timer]:
        # Mirrors the real Clock exactly: the set of due handles is fixed up
        # front in (due, id) order.  A periodic handle re-arms a fresh handle
        # before its callback runs; the re-arm is never part of the fixed set,
        # so it cannot fire twice in one pump even when its deadline would lie
        # within ``until``.
        ordered = sorted(self._timers, key=lambda t: (t.due, t.timer_id))
        due: List[_Timer] = []
        due_ids: set[int] = set()
        for timer in ordered:
            if timer.due > until:
                break
            due.append(timer)
            due_ids.add(timer.timer_id)
        self._timers = [t for t in self._timers if t.timer_id not in due_ids]
        for timer in due:
            self.now = timer.due
            if timer.periodic:
                child = self._schedule(
                    timer.interval,
                    timer.key,
                    timer.kind,
                    timer.mechanic_id,
                    periodic=True,
                    interval=timer.interval,
                )
                child.generation = timer.generation
        self.now = until
        return due

    def _cancel_key(self, key: Tuple[Any, ...]) -> int:
        count = 0
        for timer in self._timers:
            if timer.key == key:
                count += 1
        self._timers = [t for t in self._timers if t.key != key]
        return count

    def _clear_generation(self, generation: int) -> List[_Timer]:
        dropped = [t for t in self._timers if t.generation == generation]
        self._timers = [t for t in self._timers if t.generation != generation]
        return dropped

    # ------------------------------------------------------------------ #
    # Phase entry and eligibility
    # ------------------------------------------------------------------ #

    def _conditions_met(self, phase: PhaseConfig) -> bool:
        for condition in phase.entry:
            if condition.kind is ConditionType.HP_BELOW:
                boundary = self.config.boss.max_hp * condition.ref / 100.0
                if self.boss_hp > boundary:
                    return False
            elif condition.kind is ConditionType.ELAPSED:
                if self.now < condition.ref:
                    return False
            elif condition.kind is ConditionType.TARGET_STATE:
                destroyed = self.target_hp.get(condition.ref) == 0.0
                if condition.state is TargetState.DESTROYED and not destroyed:
                    return False
                if condition.state is TargetState.ALIVE and destroyed:
                    return False
        return True

    def _enter_phase(self, index: int) -> None:
        if index > 0 and self.current_phase is not None:
            self._revoke_pending_timers(self.current_phase)
            self._apply_cross_phase_policy(self.current_phase)
        self.phase_index = index
        phase = self.config.phases[index]
        self.phase_trajectory.append(
            {"time": self.now, "phase_id": phase.phase_id, "index": index}
        )
        for mechanic in sorted(phase.mechanics, key=lambda m: (m.value, m.priority)):
            if mechanic.trigger is TriggerType.TIMER:
                self._schedule(
                    max(0.0, mechanic.value - self.now),
                    ("phase_timer", phase.phase_id, mechanic.mechanic_id),
                    "phase",
                    mechanic.mechanic_id,
                    periodic=mechanic.is_periodic,
                    interval=mechanic.repeat_interval or 0.0,
                )
        self._catch_up_thresholds()
        if phase.victory_phase:
            self._victory()

    def _enter_ready_phases(self) -> None:
        while self.phase_index + 1 < len(self.config.phases):
            candidate = self.config.phases[self.phase_index + 1]
            if not self._conditions_met(candidate):
                return
            self._enter_phase(self.phase_index + 1)
            if self.status != "active":
                return

    def _eligible(self, mechanic: MechanicConfig) -> Tuple[bool, Optional[str]]:
        for target_id in mechanic.required_targets:
            if self.target_hp.get(target_id, 0.0) == 0.0:
                return False, "target_gone"
        if not self.players:
            return False, "no_living_players"
        return True, None

    # ------------------------------------------------------------------ #
    # Mechanic dispatch: the exclusivity authority
    # ------------------------------------------------------------------ #

    def _attempt_mechanic(self, mechanic: MechanicConfig, trigger: str) -> None:
        eligible, reason = self._eligible(mechanic)
        if not eligible:
            self._record_unstarted(mechanic, reason, trigger)
            return
        wanted = list(mechanic.resources)
        if mechanic.mutex_group is not None:
            wanted.append(f"group:{mechanic.mutex_group}")
        busy = next((resource for resource in wanted if resource in self._claims), None)
        if busy is not None:
            self._record_unstarted(
                mechanic, "resource_busy", trigger, busy_resource=busy
            )
            return
        self._commit(mechanic, trigger)

    def _commit(self, mechanic: MechanicConfig, trigger: str) -> None:
        instance = _Mechanic(
            mechanic_id=mechanic.mechanic_id,
            status="committed",
            start_time=self.now,
        )
        if mechanic.duration > 0:
            timer = self._schedule(
                mechanic.duration,
                ("mechanic_end", mechanic.mechanic_id),
                "end",
                mechanic.mechanic_id,
            )
            instance.end_timer_id = timer.timer_id
        self.mechanics[mechanic.mechanic_id] = instance
        for resource in mechanic.resources:
            self._claims[resource] = mechanic.mechanic_id
        if mechanic.mutex_group is not None:
            self._claims[f"group:{mechanic.mutex_group}"] = mechanic.mechanic_id
        bump(self.counters, "mechanic_start", mechanic.mechanic_id)
        if mechanic.duration <= 0:
            self._finish(instance, completed=True)

    def _record_unstarted(
        self,
        mechanic: MechanicConfig,
        reason: str,
        trigger: str,
        busy_resource: Optional[str] = None,
    ) -> None:
        status = "blocked" if reason == "resource_busy" else "revoked"
        self.mechanics[mechanic.mechanic_id] = _Mechanic(
            mechanic_id=mechanic.mechanic_id,
            status=status,
            start_time=self.now,
            end_time=self.now,
            detail=reason,
        )
        if reason == "resource_busy":
            bump(self.counters, "mechanic_blocked", mechanic.mechanic_id)
        else:
            bump(self.counters, "mechanic_revoked", mechanic.mechanic_id)

    def _finish(
        self,
        instance: _Mechanic,
        completed: bool,
        reason: Optional[str] = None,
    ) -> None:
        if not instance.active:
            return
        mechanic = self.config.mechanic(instance.mechanic_id)
        instance.status = "completed" if completed else "cancelled"
        instance.end_time = self.now
        instance.detail = reason
        for resource in mechanic.resources:
            if self._claims.get(resource) == mechanic.mechanic_id:
                del self._claims[resource]
        if mechanic.mutex_group is not None:
            group = f"group:{mechanic.mutex_group}"
            if self._claims.get(group) == mechanic.mechanic_id:
                del self._claims[group]
        if instance.end_timer_id is not None:
            self._cancel_key(("mechanic_end", mechanic.mechanic_id))
            instance.end_timer_id = None
        bump(self.counters, "mechanic_end", mechanic.mechanic_id)

    def interrupt(self, mechanic_id: str, player_id: str) -> bool:
        self._require_active()
        if player_id not in self.players:
            raise OracleError(f"unknown or dead player {player_id!r}")
        instance = self.mechanics.get(mechanic_id)
        if instance is None or not instance.active:
            bump(self.counters, "interrupt_rejected", mechanic_id)
            return False
        if not self.config.mechanic(mechanic_id).interruptible:
            bump(self.counters, "interrupt_rejected", mechanic_id)
            return False
        self._finish(instance, completed=False, reason="interrupted")
        return True

    # ------------------------------------------------------------------ #
    # Threshold progression
    # ------------------------------------------------------------------ #

    def apply_damage(self, target_id: str, amount: float, source: str) -> None:
        self._require_active()
        if amount < 0:
            raise ValueError("damage cannot be negative")
        if target_id == self.config.boss.boss_id:
            old_hp = self.boss_hp
            new_hp = max(0.0, old_hp - amount)
            self.boss_hp = new_hp
            if new_hp == 0.0:
                self._victory()
                return
            crossings = self._crossings(old_hp, new_hp)
            for mechanic in crossings:
                self._fired.add(mechanic.mechanic_id)
                owner = self.config.phase_of(mechanic.mechanic_id)
                if owner is not self.current_phase:
                    bump(self.counters, "mechanic_deferred", mechanic.mechanic_id)
                    continue
                self._attempt_mechanic(mechanic, trigger="hp_threshold")
                if self.status != "active":
                    return
            self._enter_ready_phases()
        else:
            self._damage_target(target_id, amount)

    def _crossings(self, old_hp: float, new_hp: float) -> List[MechanicConfig]:
        max_hp = self.config.boss.max_hp
        crossed: List[MechanicConfig] = []
        for phase in self.config.phases:
            for mechanic in phase.mechanics:
                if mechanic.trigger is not TriggerType.HP_THRESHOLD:
                    continue
                if mechanic.mechanic_id in self._fired:
                    continue
                boundary = max_hp * mechanic.value / 100.0
                if new_hp <= boundary < old_hp:
                    crossed.append(mechanic)
        crossed.sort(key=lambda m: (-m.value, m.priority, m.mechanic_id))
        return crossed

    def _catch_up_thresholds(self) -> None:
        candidates: List[Tuple[int, MechanicConfig]] = []
        for index, owner_phase in enumerate(self.config.phases[: self.phase_index + 1]):
            for mechanic in owner_phase.mechanics:
                if mechanic.trigger is not TriggerType.HP_THRESHOLD:
                    continue
                if mechanic.mechanic_id not in self._fired:
                    continue
                if mechanic.mechanic_id in self.mechanics:
                    continue
                candidates.append((index, mechanic))
        candidates.sort(key=lambda pair: (pair[0], pair[1].priority, pair[1].mechanic_id))
        for _index, mechanic in candidates:
            self._attempt_mechanic(mechanic, trigger="hp_threshold_catchup")
            if self.status != "active":
                return

    def _revoke_pending_timers(self, phase: PhaseConfig) -> None:
        for mechanic in phase.mechanics:
            if mechanic.trigger is not TriggerType.TIMER:
                continue
            key = ("phase_timer", phase.phase_id, mechanic.mechanic_id)
            count = self._cancel_key(key)
            if count and mechanic.mechanic_id not in self.mechanics:
                self.mechanics[mechanic.mechanic_id] = _Mechanic(
                    mechanic_id=mechanic.mechanic_id,
                    status="revoked",
                    start_time=self.now,
                    end_time=self.now,
                    detail="phase_change",
                )
                bump(self.counters, "mechanic_revoked", mechanic.mechanic_id)

    def _apply_cross_phase_policy(self, phase: PhaseConfig) -> None:
        for instance in list(self.mechanics.values()):
            if not instance.active:
                continue
            if self.config.phase_of(instance.mechanic_id) is not phase:
                continue
            mechanic = self.config.mechanic(instance.mechanic_id)
            if mechanic.cross_phase is CrossPhasePolicy.CANCEL:
                self._finish(instance, completed=False, reason="phase_change")

    # ------------------------------------------------------------------ #
    # Clock pump and timer callbacks
    # ------------------------------------------------------------------ #

    def tick(self, until: float) -> None:
        self._require_active()
        if until < self.now:
            raise ValueError("the clock cannot move backwards")
        for timer in self._pop_due(until):
            if timer.generation != self.generation:
                # Stale callback from a wiped round: counted as rejected, never
                # executed, so it cannot dispatch into the current generation.
                self.counters["timer_dropped_stale"] += 1
                continue
            if timer.kind == "end":
                instance = self.mechanics.get(timer.mechanic_id)
                if instance is not None and instance.active:
                    self._finish(instance, completed=True)
            else:
                self._on_phase_timer(timer)
            if self.status != "active":
                return
        self._enter_ready_phases()

    def _on_phase_timer(self, timer: _Timer) -> None:
        _, phase_id, mechanic_id = timer.key
        if self.status != "active":
            return
        phase = self.current_phase
        mechanic = self.config.mechanic(mechanic_id)
        if phase is None or phase.phase_id != phase_id:
            if mechanic_id not in self.mechanics:
                self._record_unstarted(mechanic, "phase_change", trigger="timer_late")
            return
        self._attempt_mechanic(mechanic, trigger="timer")

    def inject_stale_timer(self) -> None:
        """Test hook: enqueue a callback tagged with the previous generation."""

        if self.generation < 1 or self.status == "active":
            raise OracleError("stale injection is only valid while reset between rounds")
        self._schedule(
            delay=9.0,
            key=("phase_timer", "p1", "p1_channel"),
            kind="phase",
            mechanic_id="p1_channel",
        ).generation = self.generation - 1

    # ------------------------------------------------------------------ #
    # Targets, players, wipe and victory
    # ------------------------------------------------------------------ #

    def _damage_target(self, target_id: str, amount: float) -> None:
        if target_id not in self.target_hp:
            raise OracleError(f"unknown target {target_id!r}")
        old_hp = self.target_hp[target_id]
        if old_hp == 0.0:
            return
        new_hp = max(0.0, old_hp - amount)
        self.target_hp[target_id] = new_hp
        if new_hp == 0.0:
            self._destroy_target(target_id)

    def destroy_target(self, target_id: str, source: str = "scripted") -> None:
        self._require_active()
        if target_id not in self.target_hp:
            raise OracleError(f"unknown target {target_id!r}")
        if self.target_hp[target_id] == 0.0:
            return
        self.target_hp[target_id] = 0.0
        self._destroy_target(target_id)

    def _destroy_target(self, target_id: str) -> None:
        for instance in list(self.mechanics.values()):
            if not instance.active:
                continue
            mechanic = self.config.mechanic(instance.mechanic_id)
            if target_id in mechanic.required_targets:
                self._finish(instance, completed=False, reason="target_gone")
        self._enter_ready_phases()

    def player_dies(self, player_id: str) -> None:
        self._require_active()
        if player_id not in self.players:
            return
        self.players.remove(player_id)
        if not self.players:
            self._wipe()

    def _wipe(self) -> None:
        self.status = "wipe"
        self._teardown("wipe")

    def _victory(self) -> None:
        if self.status != "active":
            return
        self.status = "victory"
        self._teardown("encounter_end")

    def _teardown(self, reason: str) -> None:
        dropped = self._clear_generation(self.generation)
        self.counters["timer_cancelled"] += len(dropped)
        for instance in list(self.mechanics.values()):
            if instance.active:
                self._finish(instance, completed=False, reason=reason)
        assert not self._claims, "teardown left resource claims behind"

    def reset_after_wipe(self) -> None:
        if self.status not in ("wipe", "victory"):
            raise OracleError("reset is only valid after a wipe or victory")
        assert not self._claims
        self._timers = []
        self.now = 0.0
        self._next_timer_id = 1
        self.mechanics = {}
        self._fired = set()
        self.phase_index = -1
        self.boss_hp = self.config.boss.max_hp
        self.target_hp = {t.target_id: t.max_hp for t in self.config.targets}
        self.players = set()
        self.status = "idle"

    def _require_active(self) -> None:
        if self.status != "active":
            raise OracleError(f"encounter is not active (status={self.status!r})")

    # ------------------------------------------------------------------ #
    # Uniform action pump used by the acceptance runner
    # ------------------------------------------------------------------ #

    def step(self, action: Action) -> None:
        kind = action[0]
        if kind == "start":
            self.start_encounter()
        elif kind == "wait":
            self.tick(self.now + action[1])
        elif kind == "boss_damage":
            self.apply_damage(self.config.boss.boss_id, action[1], action[2])
        elif kind == "target_damage":
            self.apply_damage(action[1], action[2], action[3])
        elif kind == "destroy":
            self.destroy_target(action[1], source=action[2])
        elif kind == "kill":
            self.player_dies(action[1])
        elif kind == "interrupt":
            self.interrupt(action[1], action[2])
        elif kind == "inject_stale":
            self.inject_stale_timer()
        else:
            raise OracleError(f"unknown action {kind!r}")
