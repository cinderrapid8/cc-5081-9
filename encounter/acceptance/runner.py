"""The formal runner: drives the real coordinator and compares every step.

An :class:`AcceptanceRunner` wires the production
:class:`~encounter.coordinator.PhaseCoordinator` next to the independent
:class:`~encounter.acceptance.oracle.ReferenceCoordinator` and feeds both the
same :class:`~encounter.acceptance.actions.Scenario`.  After each action it
compares three things, never just the final win/lose text:

1. the phase trajectory (entered phase id and index);
2. the occupancy set (every resource/mutex claim and its holder);
3. the actual dispatch counts (starts, ends, blocks, revokes, defers, rejected
   interrupts and stale-timer drops).

The two fault switches exist solely so the suite can prove it catches real
defects.  They are off by default and only the mutation tests flip them:

* ``skip_cleanup`` -- a wipe/victory leaves one claim behind (reset-cleanup
  defect);
* ``double_dispatch`` -- one mechanic start is emitted twice (duplicate
  dispatch defect).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..clock import Clock
from ..coordinator import PhaseCoordinator
from ..events import Event, EventBus
from ..scenario import build_encounter_config
from .actions import Action, Scenario
from .oracle import ReferenceCoordinator
from .traces import (
    MECHANIC_COUNTERS,
    SCALAR_COUNTERS,
    diff_counters,
    diff_snapshot,
    empty_counters,
    normalize_snapshot,
)


class RunnerError(RuntimeError):
    pass


@dataclass
class StepRecord:
    index: int
    action: Dict[str, Any]
    time: float
    expected_snapshot: Dict[str, Any]
    actual_snapshot: Dict[str, Any]
    snapshot_diffs: List[Dict[str, Any]]
    counter_diffs: List[Dict[str, Any]]
    events: List[Dict[str, Any]]


@dataclass
class CheckResult:
    scenario_id: str
    passed: bool
    reason: Optional[str]
    expected_counters: Dict[str, Any]
    actual_counters: Dict[str, Any]
    steps: List[StepRecord] = field(default_factory=list)
    # Property assertions evaluated independently of the oracle diff.
    property_violations: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def first_failure(self) -> Optional[StepRecord]:
        for step in self.steps:
            if step.snapshot_diffs or step.counter_diffs:
                return step
        return None


class AcceptanceRunner:
    def __init__(
        self,
        config: Optional[Any] = None,
        *,
        skip_cleanup: bool = False,
        double_dispatch: bool = False,
    ) -> None:
        self.config = config or build_encounter_config()
        self.skip_cleanup = skip_cleanup
        self.double_dispatch = double_dispatch

        self.bus = EventBus()
        self.clock = Clock()
        self.coordinator = PhaseCoordinator(self.config, self.bus, self.clock)
        self.oracle = ReferenceCoordinator(self.config)
        self._events: List[Event] = []
        self._events_seen = 0
        self.bus.subscribe(self._events.append)
        self.counters: Dict[str, Any] = empty_counters()
        self.phase_trajectory: List[Dict[str, Any]] = []
        self._double_used = False

    # ------------------------------------------------------------------ #
    # Real-side observation
    # ------------------------------------------------------------------ #

    def snapshot(self) -> Dict[str, Any]:
        coordinator = self.coordinator
        return normalize_snapshot(
            {
                "time": self.clock.now,
                "generation": coordinator.generation,
                "status": coordinator.status,
                "phase_id": (
                    coordinator.current_phase.phase_id
                    if coordinator.current_phase is not None
                    else None
                ),
                "phase_index": coordinator.phase_index,
                "boss_hp": coordinator.boss_hp,
                "target_hp": dict(coordinator.target_hp),
                "players": set(coordinator.players),
                "occupancy": [
                    (claim.resource, claim.owner)
                    for claim in coordinator.resources.active_claims()
                ],
                "active_mechanics": [
                    m.mechanic_id for m in coordinator.active_mechanics
                ],
            }
        )

    def _absorb_events(self) -> None:
        """Fold newly emitted coordinator events into the comparison counters."""

        fresh = self._events[self._events_seen:]
        self._events_seen = len(self._events)
        for event in fresh:
            payload = event.payload
            if event.type == "phase_enter":
                self.phase_trajectory.append(
                    {
                        "time": event.time,
                        "phase_id": payload["phase_id"],
                        "index": payload["index"],
                    }
                )
            elif event.type in MECHANIC_COUNTERS:
                key = payload.get("mechanic_id", "?")
                self.counters[event.type][key] = self.counters[event.type].get(key, 0) + 1
                if (
                    self.double_dispatch
                    and event.type == "mechanic_start"
                    and not self._double_used
                ):
                    # The injected defect: the runner claims one mechanic
                    # dispatched twice.  No state is mutated, only the observed
                    # dispatch count, so the replay still records what the
                    # production bus actually emitted alongside the bad count.
                    self.counters[event.type][key] += 1
                    self._double_used = True
            elif event.type in SCALAR_COUNTERS:
                self.counters[event.type] += 1

    def _stale_events(self) -> List[Dict[str, Any]]:
        return [
            event.as_dict()
            for event in self._events
            if event.type == "timer_dropped_stale"
        ]

    # ------------------------------------------------------------------ #
    # Fault injection hooks
    # ------------------------------------------------------------------ #

    def _apply_skip_cleanup(self) -> None:
        if not self.skip_cleanup:
            return
        # Re-seize a resource after the coordinator tore the round down,
        # simulating an overlooked claim surviving the reset.
        self.coordinator.resources._holders["object:brazier"] = "leaked-claim"

    # ------------------------------------------------------------------ #
    # Action dispatch
    # ------------------------------------------------------------------ #

    def _step_real(self, action: Action) -> None:
        kind = action[0]
        coordinator = self.coordinator
        if kind == "start":
            coordinator.start_encounter()
        elif kind == "wait":
            coordinator.tick(self.clock.now + action[1])
        elif kind == "boss_damage":
            coordinator.apply_damage(self.config.boss.boss_id, action[1], action[2])
        elif kind == "target_damage":
            coordinator.apply_damage(action[1], action[2], action[3])
        elif kind == "destroy":
            coordinator.destroy_target(action[1], source=action[2])
        elif kind == "kill":
            coordinator.player_dies(action[1])
        elif kind == "interrupt":
            coordinator.interrupt_mechanic(action[1], action[2])
        elif kind == "inject_stale":
            self._inject_stale()
        else:
            raise RunnerError(f"unknown action {kind!r}")

        if kind in ("kill",) and coordinator.status in ("wipe", "victory"):
            self._apply_skip_cleanup()
        if kind == "boss_damage" and coordinator.status == "victory":
            self._apply_skip_cleanup()

    def _inject_stale(self) -> None:
        # Mirror the oracle hook: a timer tagged with the previous generation,
        # scheduled while the coordinator sits idle between rounds.
        if self.coordinator.generation < 1 or self.coordinator.status == "active":
            raise RunnerError("stale injection is only valid while reset between rounds")
        self.clock.schedule(
            delay=9.0,
            generation=self.coordinator.generation,
            key=("phase_timer", "p1", "p1_channel"),
            callback=lambda handle: None,
        )

    # ------------------------------------------------------------------ #
    # Scenario checking
    # ------------------------------------------------------------------ #

    def check(self, scenario: Scenario) -> CheckResult:
        result = CheckResult(
            scenario_id=scenario.scenario_id,
            passed=True,
            reason=None,
            expected_counters=empty_counters(),
            actual_counters=empty_counters(),
        )
        terminal_steps: List[int] = []
        for index, action in enumerate(scenario.actions):
            before = len(self._events)
            try:
                self.oracle.step(action)
                self._step_real(action)
            except Exception as exc:  # a crash is itself a locatable failure
                result.passed = False
                result.reason = f"step {index} raised {type(exc).__name__}: {exc}"
                result.steps.append(
                    StepRecord(
                        index=index,
                        action=_safe_action(action),
                        time=self.clock.now,
                        expected_snapshot=self.oracle.snapshot(),
                        actual_snapshot=self.snapshot(),
                        snapshot_diffs=[],
                        counter_diffs=[],
                        events=[e.as_dict() for e in self._events[before:]],
                    )
                )
                self._finalize_counts(result)
                return result

            self._absorb_events()
            if self.coordinator.status in ("wipe", "victory"):
                terminal_steps.append(index)
            expected = self.oracle.snapshot()
            actual = self.snapshot()
            step = StepRecord(
                index=index,
                action=_safe_action(action),
                time=self.clock.now,
                expected_snapshot=expected,
                actual_snapshot=actual,
                snapshot_diffs=diff_snapshot(expected, actual),
                counter_diffs=diff_counters(self.oracle.counters, self.counters),
                events=[e.as_dict() for e in self._events[before:]],
            )
            result.steps.append(step)
            if step.snapshot_diffs or step.counter_diffs:
                result.passed = False

        self._finalize_counts(result)
        result.property_violations = self._property_checks(scenario, terminal_steps)
        if result.property_violations:
            result.passed = False
            if result.reason is None:
                result.reason = f"{len(result.property_violations)} property violation(s)"
        if not result.passed and result.reason is None:
            failure = result.first_failure
            result.reason = (
                f"first divergence at step {failure.index}" if failure else "unknown divergence"
            )
        return result

    def _finalize_counts(self, result: CheckResult) -> None:
        result.expected_counters = _copy_counters(self.oracle.counters)
        result.actual_counters = _copy_counters(self.counters)

    def _property_checks(
        self, scenario: Scenario, terminal_steps: List[int]
    ) -> List[Dict[str, Any]]:
        """Independent invariants evaluated straight off production state."""

        violations: List[Dict[str, Any]] = []
        # Reset-cleanup: after every wipe/victory the production coordinator
        # must hold no claims and no committed mechanics.
        coordinator = self.coordinator
        if terminal_steps:
            last_terminal = terminal_steps[-1]
            if coordinator.status in ("wipe", "victory", "idle"):
                if not coordinator.resources.is_empty():
                    violations.append(
                        {
                            "property": "reset",
                            "step": last_terminal,
                            "detail": "occupancy survived a terminal round",
                            "claims": [
                                {"resource": c.resource, "owner": c.owner}
                                for c in coordinator.resources.active_claims()
                            ],
                        }
                    )
                if coordinator.active_mechanics:
                    violations.append(
                        {
                            "property": "reset",
                            "step": last_terminal,
                            "detail": "active mechanics survived a terminal round",
                            "mechanics": [
                                m.mechanic_id for m in coordinator.active_mechanics
                            ],
                        }
                    )
        # Exclusivity: a resource is never handed to a second mechanic while a
        # first still holds it.  mechanic_end events release before any later
        # start, so tracking live holders from the event stream proves it.
        live_holders: Dict[str, str] = {}
        for event in self._events:
            if event.type == "mechanic_start":
                mechanic_id = event.payload["mechanic_id"]
                for resource in event.payload["resources"]:
                    holder = live_holders.get(resource)
                    if holder is not None and holder != mechanic_id:
                        violations.append(
                            {
                                "property": "exclusivity",
                                "detail": f"{resource} re-dispatched while held",
                                "holders": [holder, mechanic_id],
                            }
                        )
                    live_holders[resource] = mechanic_id
                if event.payload.get("mutex_group"):
                    group = f"group:{event.payload['mutex_group']}"
                    holder = live_holders.get(group)
                    if holder is not None and holder != mechanic_id:
                        violations.append(
                            {
                                "property": "exclusivity",
                                "detail": f"mutex group entered concurrently for {group}",
                                "holders": [holder, mechanic_id],
                            }
                        )
                    live_holders[group] = mechanic_id
            elif event.type == "mechanic_end":
                for resource in event.payload["resources"]:
                    live_holders.pop(resource, None)
        # Phase eligibility: the recorded phase index must advance one at a
        # time, never skip and never go backwards, within each generation.
        phase_events = [e for e in self._events if e.type == "phase_enter"]
        per_generation: Dict[int, List[int]] = {}
        for event in phase_events:
            per_generation.setdefault(event.payload["generation"], []).append(
                event.payload["index"]
            )
        for generation, indices in per_generation.items():
            if indices != list(range(len(indices))):
                violations.append(
                    {
                        "property": "phase",
                        "detail": "phase indices did not advance strictly 0..n",
                        "generation": generation,
                        "indices": indices,
                    }
                )
        # Stale-callback rejection: every injected old-generation timer must be
        # dropped explicitly and never dispatch a mechanic.
        if any(a[0] == "inject_stale" for a in scenario.actions):
            dropped = self._stale_events()
            if not dropped:
                violations.append(
                    {
                        "property": "reset",
                        "detail": "injected stale callback was never rejected",
                    }
                )
            for event in dropped:
                payload = event["payload"]
                if payload["timer_generation"] >= payload["current_generation"]:
                    violations.append(
                        {
                            "property": "reset",
                            "detail": "stale drop recorded with a non-stale generation",
                            "event": event,
                        }
                    )
        return violations


def _copy_counters(counters: Dict[str, Any]) -> Dict[str, Any]:
    return {
        name: (dict(value) if name in MECHANIC_COUNTERS else value)
        for name, value in counters.items()
    }


def _safe_action(action: Action) -> Dict[str, Any]:
    from .actions import action_to_dict

    try:
        return action_to_dict(action)
    except ValueError:
        return {"kind": action[0], "raw": [str(part) for part in action[1:]]}
