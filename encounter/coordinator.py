"""The phase coordinator: the only authority on encounter state.

Design points this module exists to prove:

* Phases advance when *all* their entry conditions hold (boss HP thresholds,
  target state, elapsed time).  Animation completion is never a condition.
* One damage event may cross several thresholds.  They are processed one at a
  time in descending order, re-checking eligibility after each phase change so
  mutually exclusive mechanics can never start simultaneously.
* Committed mechanics carry an explicit cross-phase policy.  Unstarted timer
  mechanics are revoked when their phase ends; there is no implicit cleanup.
* Every timer carries the encounter generation.  A callback arriving after a
  wipe/reset is dropped as stale instead of touching the new round.
* Every observable change leaves the coordinator as an event on the bus.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .clock import Clock, TimerHandle
from .config import (
    ConditionType,
    CrossPhasePolicy,
    EncounterConfig,
    MechanicConfig,
    PhaseConfig,
    TargetState,
    TriggerType,
)
from .events import EventBus
from .mechanic import MechanicInstance, MechanicStatus, RevokeReason
from .resources import ResourceManager


class CoordinatorError(RuntimeError):
    pass


class PhaseCoordinator:
    def __init__(self, config: EncounterConfig, bus: EventBus, clock: Clock) -> None:
        config.validate()
        self.config = config
        self.bus = bus
        self.clock = clock
        self.resources = ResourceManager()

        self.generation = 0
        self.status = "idle"
        self.phase_index = -1
        self.phase_start_time = 0.0
        self.boss_hp = config.boss.max_hp
        self.target_hp: Dict[str, float] = {t.target_id: t.max_hp for t in config.targets}
        self.players: Set[str] = {f"player-{i + 1}" for i in range(config.player_slots)}
        self.mechanics: Dict[str, MechanicInstance] = {}
        self._fired_thresholds: Set[str] = set()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start_encounter(self) -> None:
        if self.status == "active":
            raise CoordinatorError("encounter already active")
        if self.generation > 0:
            # Restarting after a wipe: prove the previous round left nothing
            # live behind.  Finished/blocked instances may remain as records.
            assert self.resources.is_empty(), "reset with lingering resource claims"
            assert not self.active_mechanics, "reset with lingering active mechanics"
        self.generation += 1
        self.status = "active"
        self.boss_hp = self.config.boss.max_hp
        self.target_hp = {t.target_id: t.max_hp for t in self.config.targets}
        self.players = {f"player-{i + 1}" for i in range(self.config.player_slots)}
        self.mechanics = {}
        self._fired_thresholds = set()
        self.phase_index = -1
        self.phase_start_time = 0.0
        self.bus.publish(
            "encounter_start",
            self.clock.now,
            generation=self.generation,
            encounter_id=self.config.encounter_id,
            players=sorted(self.players),
        )
        self._enter_phase(0)

    @property
    def current_phase(self) -> Optional[PhaseConfig]:
        if 0 <= self.phase_index < len(self.config.phases):
            return self.config.phases[self.phase_index]
        return None

    @property
    def active_mechanics(self) -> List[MechanicInstance]:
        return [m for m in self.mechanics.values() if m.active]

    # ------------------------------------------------------------------ #
    # Damage and threshold progression
    # ------------------------------------------------------------------ #

    def apply_damage(self, target_id: str, amount: float, source: str) -> None:
        """Apply damage to the boss or a target, processing thresholds stepwise."""

        self._require_active()
        if amount < 0:
            raise ValueError("damage cannot be negative")
        if target_id == self.config.boss.boss_id:
            old_hp = self.boss_hp
            new_hp = max(0.0, old_hp - amount)
            self.boss_hp = new_hp
            self.bus.publish(
                "damage",
                self.clock.now,
                target=target_id,
                amount=amount,
                source=source,
                hp_before=old_hp,
                hp_after=new_hp,
            )
            if new_hp == 0.0:
                self._victory()
                return
            crossings = self._collect_threshold_crossings(old_hp, new_hp)
            # One threshold at a time; each pass may change the phase, which
            # changes which mechanics are even eligible.
            for mechanic in crossings:
                self._process_threshold(mechanic)
                if self.status != "active":
                    return
            self._enter_ready_phases()
        else:
            self._damage_target(target_id, amount, source)

    def _collect_threshold_crossings(
        self, old_hp: float, new_hp: float
    ) -> List[MechanicConfig]:
        max_hp = self.config.boss.max_hp
        crossed: List[MechanicConfig] = []
        for phase in self.config.phases:
            for mechanic in phase.mechanics:
                if mechanic.trigger is not TriggerType.HP_THRESHOLD:
                    continue
                if mechanic.mechanic_id in self._fired_thresholds:
                    continue
                boundary = max_hp * mechanic.value / 100.0
                # Descending HP: crossed when the boundary lies inside [new, old).
                if new_hp <= boundary < old_hp:
                    crossed.append(mechanic)
        # Highest HP percentage first, priority as a deterministic tiebreaker.
        crossed.sort(key=lambda m: (-m.value, m.priority, m.mechanic_id))
        return crossed

    def _process_threshold(self, mechanic: MechanicConfig) -> None:
        self._fired_thresholds.add(mechanic.mechanic_id)
        owner_phase = self.config.phase_of(mechanic.mechanic_id)
        if owner_phase is not self.current_phase:
            # The phase that owns this mechanic has not been entered yet.  It
            # is remembered and retried through the catch-up path once entered.
            self.bus.publish(
                "mechanic_deferred",
                self.clock.now,
                mechanic_id=mechanic.mechanic_id,
                reason="phase_not_entered",
            )
            return
        self._attempt_mechanic(mechanic, trigger="hp_threshold")

    # ------------------------------------------------------------------ #
    # Phase entry
    # ------------------------------------------------------------------ #

    def _enter_ready_phases(self) -> None:
        """Enter every phase whose conditions are currently satisfied.

        A loop rather than a single check: one damage event can satisfy the
        HP condition of several phases at once, and later phases may also be
        unblocked by the target destruction handled inside entry.
        """

        while self.phase_index + 1 < len(self.config.phases):
            candidate = self.config.phases[self.phase_index + 1]
            if not self._conditions_met(candidate):
                return
            self._enter_phase(self.phase_index + 1)
            if self.status != "active":
                return

    def _conditions_met(self, phase: PhaseConfig) -> bool:
        for condition in phase.entry:
            if condition.kind is ConditionType.HP_BELOW:
                boundary = self.config.boss.max_hp * condition.ref / 100.0
                if self.boss_hp > boundary:
                    return False
            elif condition.kind is ConditionType.ELAPSED:
                if self.clock.now < condition.ref:
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
            self._revoke_pending_timers(self.current_phase, RevokeReason.PHASE_CHANGE)
            self._apply_cross_phase_policy(self.current_phase)
        self.phase_index = index
        phase = self.config.phases[index]
        self.phase_start_time = self.clock.now
        self.bus.publish(
            "phase_enter",
            self.clock.now,
            phase_id=phase.phase_id,
            phase_name=phase.name,
            index=index,
            generation=self.generation,
        )
        # Timers belonging to the new phase.
        for mechanic in sorted(phase.mechanics, key=lambda m: (m.value, m.priority)):
            if mechanic.trigger is TriggerType.TIMER:
                self._schedule_timer_mechanic(mechanic, phase)
        # Threshold mechanics whose boundary was crossed while their owning
        # phase had not been entered yet.
        self._catch_up_thresholds()
        if phase.victory_phase:
            self._victory()

    def _catch_up_thresholds(self) -> None:
        candidates: List[Tuple[int, MechanicConfig]] = []
        for index, owner_phase in enumerate(self.config.phases[: self.phase_index + 1]):
            for mechanic in owner_phase.mechanics:
                if mechanic.trigger is not TriggerType.HP_THRESHOLD:
                    continue
                if mechanic.mechanic_id not in self._fired_thresholds:
                    continue
                if mechanic.mechanic_id in self.mechanics:
                    continue
                candidates.append((index, mechanic))
        # Earlier phases first (their windows are closing), then priority,
        # so a single big hit never launches two mutually exclusive mechanics.
        candidates.sort(key=lambda pair: (pair[0], pair[1].priority, pair[1].mechanic_id))
        for index, mechanic in candidates:
            self._attempt_mechanic(mechanic, trigger="hp_threshold_catchup")
            if self.status != "active":
                return

    # ------------------------------------------------------------------ #
    # Mechanic eligibility, claiming and termination
    # ------------------------------------------------------------------ #

    def _eligible(self, mechanic: MechanicConfig) -> Tuple[bool, Optional[str]]:
        for target_id in mechanic.required_targets:
            if self.target_hp.get(target_id, 0.0) == 0.0:
                return False, RevokeReason.TARGET_GONE.value
        if not self.players:
            return False, "no_living_players"
        return True, None

    def _attempt_mechanic(self, mechanic: MechanicConfig, trigger: str) -> None:
        eligible, reason = self._eligible(mechanic)
        if not eligible:
            self._record_unstarted(mechanic, RevokeReason(reason), trigger)
            return
        ok, resource, holder = self.resources.try_claim(
            mechanic.mechanic_id, mechanic.resources, mechanic.mutex_group
        )
        if not ok:
            # Deterministic contention policy: skip, never queue behind the
            # holder, and publish exactly why.
            self._record_unstarted(
                mechanic,
                RevokeReason.RESOURCE_BUSY,
                trigger,
                resource=resource,
                holder=holder,
            )
            return
        self._commit_mechanic(mechanic, trigger)

    def _commit_mechanic(self, mechanic: MechanicConfig, trigger: str) -> None:
        instance = MechanicInstance(
            config=mechanic,
            status=MechanicStatus.COMMITTED,
            start_time=self.clock.now,
            generation=self.generation,
        )
        if mechanic.duration > 0:
            handle = self.clock.schedule(
                mechanic.duration,
                self.generation,
                ("mechanic_end", mechanic.mechanic_id),
                self._on_mechanic_end_timer,
            )
            instance.end_timer_id = handle.timer_id
        self.mechanics[mechanic.mechanic_id] = instance
        self.bus.publish(
            "mechanic_start",
            self.clock.now,
            mechanic_id=mechanic.mechanic_id,
            name=mechanic.name,
            phase_id=self.config.phase_of(mechanic.mechanic_id).phase_id,
            trigger=trigger,
            resources=list(mechanic.resources),
            mutex_group=mechanic.mutex_group,
            duration=mechanic.duration,
            cross_phase=mechanic.cross_phase.value,
            generation=self.generation,
        )
        if mechanic.duration <= 0:
            self._finish_mechanic(instance, completed=True)

    def _record_unstarted(
        self,
        mechanic: MechanicConfig,
        reason: RevokeReason,
        trigger: str,
        **extra: Any,
    ) -> None:
        instance = MechanicInstance(
            config=mechanic,
            status=MechanicStatus.BLOCKED
            if reason is RevokeReason.RESOURCE_BUSY
            else MechanicStatus.REVOKED,
            start_time=self.clock.now,
            end_time=self.clock.now,
            generation=self.generation,
            detail=reason.value,
        )
        self.mechanics[mechanic.mechanic_id] = instance
        self.bus.publish(
            "mechanic_blocked" if reason is RevokeReason.RESOURCE_BUSY else "mechanic_revoked",
            self.clock.now,
            mechanic_id=mechanic.mechanic_id,
            name=mechanic.name,
            reason=reason.value,
            trigger=trigger,
            generation=self.generation,
            **extra,
        )

    def _finish_mechanic(
        self,
        instance: MechanicInstance,
        completed: bool,
        reason: Optional[RevokeReason] = None,
        actor: Optional[str] = None,
    ) -> None:
        mechanic = instance.config
        if not instance.active:
            return
        instance.status = MechanicStatus.COMPLETED if completed else MechanicStatus.CANCELLED
        instance.end_time = self.clock.now
        instance.detail = reason.value if reason else None
        self.resources.release(
            mechanic.mechanic_id, mechanic.resources, mechanic.mutex_group
        )
        if instance.end_timer_id is not None:
            self.clock.cancel_key(("mechanic_end", mechanic.mechanic_id))
            instance.end_timer_id = None
        self.bus.publish(
            "mechanic_end",
            self.clock.now,
            mechanic_id=mechanic.mechanic_id,
            name=mechanic.name,
            outcome=instance.status.value,
            reason=reason.value if reason else "duration_elapsed",
            actor=actor,
            resources=list(mechanic.resources),
            generation=self.generation,
        )
        # Periodic re-arming is owned by the Clock (it re-schedules before the
        # callback runs).  When the phase ends its pending timer is revoked via
        # its ("phase_timer", ...) key in _revoke_pending_timers.

    def interrupt_mechanic(self, mechanic_id: str, player_id: str) -> bool:
        """A test character interrupts a channeling mechanic."""

        self._require_active()
        if player_id not in self.players:
            raise CoordinatorError(f"unknown or dead player {player_id!r}")
        instance = self.mechanics.get(mechanic_id)
        if instance is None or not instance.active:
            self.bus.publish(
                "interrupt_rejected",
                self.clock.now,
                mechanic_id=mechanic_id,
                player=player_id,
                reason="not_active",
            )
            return False
        if not instance.config.interruptible:
            self.bus.publish(
                "interrupt_rejected",
                self.clock.now,
                mechanic_id=mechanic_id,
                player=player_id,
                reason="not_interruptible",
            )
            return False
        self._finish_mechanic(instance, completed=False, reason=RevokeReason.INTERRUPTED, actor=player_id)
        return True

    # ------------------------------------------------------------------ #
    # Timer mechanics and the tick pump
    # ------------------------------------------------------------------ #

    def _schedule_timer_mechanic(
        self, mechanic: MechanicConfig, phase: PhaseConfig
    ) -> None:
        key = ("phase_timer", phase.phase_id, mechanic.mechanic_id)
        self.clock.schedule(
            max(0.0, mechanic.value - self.clock.now),
            self.generation,
            key,
            self._on_phase_timer,
            periodic=mechanic.is_periodic,
            interval=mechanic.repeat_interval or 0.0,
        )

    def _revoke_pending_timers(self, phase: PhaseConfig, reason: RevokeReason) -> None:
        for mechanic in phase.mechanics:
            if mechanic.trigger is not TriggerType.TIMER:
                continue
            key = ("phase_timer", phase.phase_id, mechanic.mechanic_id)
            count = self.clock.cancel_key(key)
            if count and mechanic.mechanic_id not in self.mechanics:
                # Only unstarted mechanics are revoked here; committed ones
                # are handled by the explicit cross-phase policy.
                instance = MechanicInstance(
                    config=mechanic,
                    status=MechanicStatus.REVOKED,
                    start_time=self.clock.now,
                    end_time=self.clock.now,
                    generation=self.generation,
                    detail=reason.value,
                )
                self.mechanics[mechanic.mechanic_id] = instance
                self.bus.publish(
                    "mechanic_revoked",
                    self.clock.now,
                    mechanic_id=mechanic.mechanic_id,
                    name=mechanic.name,
                    reason=reason.value,
                    trigger="timer",
                    generation=self.generation,
                )

    def _apply_cross_phase_policy(self, phase: PhaseConfig) -> None:
        for instance in list(self.mechanics.values()):
            if not instance.active:
                continue
            if self.config.phase_of(instance.mechanic_id) is not phase:
                continue
            if instance.config.cross_phase is CrossPhasePolicy.CANCEL:
                self._finish_mechanic(
                    instance, completed=False, reason=RevokeReason.PHASE_CHANGE
                )
            # CONTINUE: keep running and keep holding its claims; an explicit
            # policy event makes the commitment visible in the run record.
            else:
                self.bus.publish(
                    "mechanic_carried_over",
                    self.clock.now,
                    mechanic_id=instance.mechanic_id,
                    from_phase=phase.phase_id,
                    generation=self.generation,
                )

    def tick(self, until: float) -> None:
        """Advance the virtual clock and dispatch every due timer."""

        self._require_active()
        for handle in self.clock.advance(until):
            if handle.generation != self.generation:
                self.bus.publish(
                    "timer_dropped_stale",
                    self.clock.now,
                    timer_id=handle.timer_id,
                    timer_generation=handle.generation,
                    current_generation=self.generation,
                    key=list(handle.key),
                )
                continue
            handle.callback(handle)
            if self.status != "active":
                return
        self._enter_ready_phases()

    def _on_phase_timer(self, handle: TimerHandle) -> None:
        _, phase_id, mechanic_id = handle.key
        if self.status != "active":
            return
        phase = self.current_phase
        if phase is None or phase.phase_id != phase_id:
            # Phase ended before the callback ran: the pending action is
            # revoked rather than silently dropped.
            mechanic = self.config.mechanic(mechanic_id)
            if mechanic_id not in self.mechanics:
                self._record_unstarted(
                    mechanic, RevokeReason.PHASE_CHANGE, trigger="timer_late"
                )
            return
        mechanic = self.config.mechanic(mechanic_id)
        self._attempt_mechanic(mechanic, trigger="timer")

    def _on_mechanic_end_timer(self, handle: TimerHandle) -> None:
        _, mechanic_id = handle.key
        instance = self.mechanics.get(mechanic_id)
        if instance is not None and instance.active:
            self._finish_mechanic(instance, completed=True)

    # ------------------------------------------------------------------ #
    # Targets, players, wipe and victory
    # ------------------------------------------------------------------ #

    def _damage_target(self, target_id: str, amount: float, source: str) -> None:
        if target_id not in self.target_hp:
            raise CoordinatorError(f"unknown target {target_id!r}")
        old_hp = self.target_hp[target_id]
        if old_hp == 0.0:
            return
        new_hp = max(0.0, old_hp - amount)
        self.target_hp[target_id] = new_hp
        self.bus.publish(
            "damage",
            self.clock.now,
            target=target_id,
            amount=amount,
            source=source,
            hp_before=old_hp,
            hp_after=new_hp,
        )
        if new_hp == 0.0:
            self._destroy_target(target_id)

    def destroy_target(self, target_id: str, source: str = "scripted") -> None:
        """Explicit trigger used by the test characters."""

        self._require_active()
        if target_id not in self.target_hp:
            raise CoordinatorError(f"unknown target {target_id!r}")
        if self.target_hp[target_id] == 0.0:
            return
        old_hp = self.target_hp[target_id]
        self.target_hp[target_id] = 0.0
        self.bus.publish(
            "damage",
            self.clock.now,
            target=target_id,
            amount=old_hp,
            source=source,
            hp_before=old_hp,
            hp_after=0.0,
        )
        self._destroy_target(target_id)

    def _destroy_target(self, target_id: str) -> None:
        self.bus.publish(
            "target_destroyed",
            self.clock.now,
            target=target_id,
            generation=self.generation,
        )
        # Mechanics that required this object lose eligibility immediately.
        for instance in list(self.mechanics.values()):
            if instance.active and target_id in instance.config.required_targets:
                self._finish_mechanic(
                    instance, completed=False, reason=RevokeReason.TARGET_GONE
                )
        self._enter_ready_phases()

    def player_dies(self, player_id: str) -> None:
        self._require_active()
        if player_id not in self.players:
            return
        self.players.remove(player_id)
        self.bus.publish(
            "player_down", self.clock.now, player=player_id, alive=sorted(self.players)
        )
        if not self.players:
            self._wipe()

    def player_revived(self, player_id: str) -> None:
        """Mid-encounter revive (slots only come back wholesale on reset)."""

        self._require_active()
        if player_id in self.players:
            return
        known = {f"player-{i + 1}" for i in range(self.config.player_slots)}
        if player_id not in known:
            raise CoordinatorError(f"unknown player {player_id!r}")
        self.players.add(player_id)
        self.bus.publish(
            "player_revived", self.clock.now, player=player_id, alive=sorted(self.players)
        )

    def _wipe(self) -> None:
        self.status = "wipe"
        self.bus.publish("wipe", self.clock.now, generation=self.generation)
        self._teardown_round(RevokeReason.WIPE)

    def _victory(self) -> None:
        if self.status != "active":
            return
        self.status = "victory"
        self.bus.publish(
            "victory",
            self.clock.now,
            boss_hp=self.boss_hp,
            generation=self.generation,
        )
        self._teardown_round(RevokeReason.ENCOUNTER_END)

    def _teardown_round(self, reason: RevokeReason) -> None:
        # Pending timers are revoked first, then committed mechanics release
        # every claim.  A wipe generation's timers are cancelled wholesale so
        # their later firing is provably stale.
        dropped = self.clock.clear_generation(self.generation)
        for handle in dropped:
            self.bus.publish(
                "timer_cancelled",
                self.clock.now,
                timer_id=handle.timer_id,
                key=list(handle.key),
                reason=reason.value,
            )
        for instance in list(self.mechanics.values()):
            if instance.active:
                self._finish_mechanic(instance, completed=False, reason=reason)
        assert self.resources.is_empty(), "teardown left resource claims behind"
        self.bus.publish(
            "round_clean",
            self.clock.now,
            generation=self.generation,
            claims=[c.resource for c in self.resources.active_claims()],
            status=self.status,
        )

    def reset_after_wipe(self) -> None:
        """Return to the idle state so a fresh round can be started."""

        if self.status not in ("wipe", "victory"):
            raise CoordinatorError("reset is only valid after a wipe or victory")
        assert self.resources.is_empty()
        self.clock.reset()
        self.mechanics = {}
        self._fired_thresholds = set()
        self.phase_index = -1
        self.phase_start_time = 0.0
        self.boss_hp = self.config.boss.max_hp
        self.target_hp = {t.target_id: t.max_hp for t in self.config.targets}
        self.players = set()
        self.status = "idle"
        self.bus.publish("encounter_reset", 0.0, generation=self.generation)

    def _require_active(self) -> None:
        if self.status != "active":
            raise CoordinatorError(f"encounter is not active (status={self.status!r})")
