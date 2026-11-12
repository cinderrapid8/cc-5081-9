"""Bounded scenario generation and the explicit coverage ledger.

Two complementary feeds produce the acceptance scenarios, both fully
deterministic (no random seed anywhere):

* a fixed set of handcrafted contracts aimed at the dangerous corners -- big
  damage crossing several thresholds at once, cancellation arriving in the
  same pump as natural completion, repeated wipe/restart rounds and stale
  callbacks from a dead generation;
* a bounded depth-first enumeration of short legal action sequences.  The
  search is pruned by an abstract state signature so equivalent prefixes are
  not re-explored, and both depth and node counts are capped.

Because the enumeration cannot cover the whole state space,
:func:`coverage_ledger` records exactly which (state x feature) combinations
were exercised and lists the ones that were not.  A passing run therefore comes
with an honest statement of its limits instead of a single lucky seeded trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from ..scenario import BRAZIER, WARDSTONE, build_encounter_config
from .actions import (
    ACTOR_IDS,
    BOSS_DAMAGES,
    TARGET_DAMAGES,
    WAITS,
    Action,
    Scenario,
)

# Enumeration bounds.  Kept small on purpose: the point is broad coverage of
# decision points, not exhaustive search.
MAX_DEPTH = 5
MAX_NODES = 220

# Tracked state dimensions for the coverage ledger.
PHASE_BUCKETS = ("idle", "p1", "p2", "p3", "terminal")
HP_BUCKETS = ("full", "high", "mid", "low", "zero")
OCCUPANCY_BUCKETS = ("empty", "single", "contended", "multi")
PLAYERS_BUCKETS = ("all", "partial", "none")
ROUND_BUCKETS = ("round1", "round2plus")

# The feasible subset of the full cartesian product.  Combinations outside
# these constraints cannot occur in the shipped encounter (e.g. a terminal
# round always reports empty occupancy, and an idle fight has full boss HP), so
# listing them as "uncovered" would be noise rather than a coverage gap.
FEASIBLE_RULES = (
    ("idle", ("full",), ("empty",), ("all",), ("round1",)),
    ("terminal", ("zero",), ("empty",), ("none",), ("round1", "round2plus")),
    ("p1", ("full", "high", "mid"), ("empty", "single", "contended", "multi"),
     ("all", "partial"), ("round1", "round2plus")),
    ("p2", ("mid", "low"), ("empty", "single", "contended", "multi"),
     ("all", "partial"), ("round1", "round2plus")),
    ("p3", ("low",), ("empty", "single", "contended", "multi"),
     ("all", "partial"), ("round1", "round2plus")),
)

FEATURES = (
    "big_damage_multi_threshold",
    "same_tick_cancel_and_complete",
    "stale_callback_rejected",
    "multiple_wipe_restart",
    "overlapping_threshold_priority",
    "target_gated_phase",
    "mutex_contention_block",
    "periodic_timer",
    "interrupt_channel",
    "cross_phase_carry",
    "cross_phase_cancel",
)


# --------------------------------------------------------------------------- #
# Handcrafted edge-case contracts
# --------------------------------------------------------------------------- #

def handcrafted_scenarios() -> List[Scenario]:
    scenarios: List[Scenario] = [
        Scenario(
            "edge_big_damage_crosses_all_thresholds",
            "One 560-point hit crosses 80%, 60% and the P2-owned 45% boundary "
            "in a single damage event; thresholds resolve one at a time and the "
            "out-of-phase mechanic is deferred rather than started.",
            [
                ("start",),
                ("boss_damage", 560, "player-2"),
                ("wait", 3.0),
                ("destroy", WARDSTONE, "player-1"),
            ],
            features=[
                "big_damage_multi_threshold",
                "overlapping_threshold_priority",
                "target_gated_phase",
            ],
        ),
        Scenario(
            "edge_lethal_damage_skips_straight_to_victory",
            "A single 1000-point hit reduces the boss to zero; victory teardown "
            "runs even though intermediate thresholds were jumped over.",
            [("start",), ("boss_damage", 1000, "player-2")],
            features=["big_damage_multi_threshold"],
        ),
        Scenario(
            "edge_cancel_and_natural_completion_same_tick",
            "The 10s channel completes on its end timer at t=15; an interrupt "
            "attempted in the same pump afterwards is rejected because the "
            "channel is already gone.  A phase-change cancel of another "
            "mechanic lands in the same overall window.",
            [
                ("start",),
                ("wait", 5.0),    # channel commits at t=5
                ("wait", 10.0),   # channel end timer due at t=15 -> completed
                ("interrupt", "p1_channel", "player-3"),  # rejected, same pump
                ("boss_damage", 450, "player-2"),
                ("destroy", WARDSTONE, "player-1"),  # enter P2, cancel pulse
            ],
            features=[
                "same_tick_cancel_and_complete",
                "interrupt_channel",
                "cross_phase_cancel",
            ],
        ),
        Scenario(
            "edge_interrupt_right_before_natural_end",
            "An interrupt one second before the channel would end cancels it "
            "and frees the dais; no second end event is dispatched later.",
            [
                ("start",),
                ("wait", 5.0),
                ("wait", 9.0),
                ("interrupt", "p1_channel", "player-3"),
                ("wait", 3.0),
            ],
            features=["interrupt_channel"],
        ),
        Scenario(
            "edge_two_wipes_and_three_rounds",
            "Wipe/reset/restart twice in a row; every new round starts with no "
            "claims, full boss HP and an independent generation.",
            [
                ("start",),
                ("boss_damage", 200, "player-2"),
                ("kill", "player-1"),
                ("kill", "player-2"),
                ("kill", "player-3"),
                ("start",),
                ("boss_damage", 250, "player-2"),
                ("kill", "player-1"),
                ("kill", "player-2"),
                ("kill", "player-3"),
                ("start",),
                ("boss_damage", 1000, "player-2"),
            ],
            features=["multiple_wipe_restart"],
        ),
        Scenario(
            "edge_stale_callback_from_wiped_round_rejected",
            "A timer tagged with the wiped generation is injected while the "
            "coordinator is idle, then a new round starts and pumps past its "
            "deadline; the callback is dropped as stale and dispatches nothing.",
            [
                ("start",),
                ("kill", "player-1"),
                ("kill", "player-2"),
                ("kill", "player-3"),
                ("inject_stale",),
                ("start",),
                ("wait", 10.0),
            ],
            features=["stale_callback_rejected", "multiple_wipe_restart"],
        ),
        Scenario(
            "edge_overlapping_boundary_priority_and_block",
            "Two mechanics share the exact 80% boundary and object; priority "
            "breaks the tie deterministically and the loser is blocked, never "
            "queued, leaving no partial occupancy.",
            [("start",), ("boss_damage", 200, "player-2"), ("wait", 1.0)],
            features=[
                "overlapping_threshold_priority",
                "mutex_contention_block",
            ],
        ),
        Scenario(
            "edge_target_gate_holds_then_releases",
            "HP alone crosses the P2 gate but the wardstone stands; the phase "
            "stays locked until the target is destroyed, and the deferred "
            "threshold mechanic replays through contention at entry.",
            [
                ("start",),
                ("boss_damage", 560, "player-2"),
                ("wait", 1.0),
                ("destroy", WARDSTONE, "player-1"),
                ("wait", 2.0),
            ],
            features=["target_gated_phase", "mutex_contention_block"],
        ),
        Scenario(
            "edge_periodic_pulse_across_phase_change",
            "The periodic pulse re-arms three times under incremental pumps "
            "(each 2s pulse ends before its 8s re-arm fires), then the phase "
            "change cancels both the pending timer and the committed instance.",
            [
                ("start",),
                ("wait", 8.0),
                ("wait", 8.0),
                ("wait", 8.0),
                ("wait", 2.0),
                ("boss_damage", 450, "player-2"),
                ("destroy", WARDSTONE, "player-1"),
            ],
            features=["periodic_timer", "cross_phase_cancel"],
        ),
        Scenario(
            "edge_carry_over_mechanic_survives_phase",
            "The CONTINUE-policy seal keeps its claims into P2 and blocks a "
            "later mutex-group mechanic on disjoint resources.",
            [
                ("start",),
                ("boss_damage", 200, "player-2"),
                ("boss_damage", 250, "player-2"),
                ("destroy", WARDSTONE, "player-1"),
                ("boss_damage", 400, "player-2"),
            ],
            features=["cross_phase_carry", "mutex_contention_block"],
        ),
        Scenario(
            "edge_target_damage_softens_before_destroy",
            "Partial target damage never destroys the wardstone; the phase gate "
            "only releases once an explicit destruction lands.",
            [
                ("start",),
                ("target_damage", WARDSTONE, 80, "player-1"),
                ("target_damage", WARDSTONE, 80, "player-1"),
                ("boss_damage", 450, "player-2"),
                ("wait", 1.0),
                ("destroy", WARDSTONE, "player-1"),
            ],
            features=["target_gated_phase"],
        ),
    ]
    return scenarios


# --------------------------------------------------------------------------- #
# Bounded enumeration
# --------------------------------------------------------------------------- #

@dataclass
class _SearchState:
    started: bool
    status: str
    phase_index: int
    hp_bucket: str
    wardstone_dead: bool
    brazier_dead: bool
    players_alive: int
    occupancy_count: int
    generation: int

    def signature(self) -> Tuple[Any, ...]:
        return (
            self.started,
            self.status,
            self.phase_index,
            self.hp_bucket,
            self.wardstone_dead,
            self.brazier_dead,
            self.players_alive,
            min(self.occupancy_count, 3),
            min(self.generation, 2),
        )


def _hp_bucket(hp: float, max_hp: float) -> str:
    ratio = hp / max_hp
    if hp <= 0:
        return "zero"
    if ratio > 0.8:
        return "full"
    if ratio > 0.6:
        return "high"
    if ratio > 0.3:
        return "mid"
    return "low"


def _legal_actions(state: _SearchState) -> List[Action]:
    actions: List[Action] = []
    if not state.started or state.status in ("wipe", "victory"):
        # Restart only makes sense after a finished round; an unstarted fight
        # gets exactly one opener.
        if not state.started:
            return [("start",)]
        actions.append(("start",))
        return actions
    if state.status != "active":
        return actions
    for amount in BOSS_DAMAGES:
        actions.append(("boss_damage", amount, "player-2"))
    for wait in WAITS:
        actions.append(("wait", wait))
    for target in (WARDSTONE, BRAZIER):
        for amount in TARGET_DAMAGES:
            actions.append(("target_damage", target, amount, "player-1"))
        actions.append(("destroy", target, "player-1"))
    # Only interrupting the channel is legal in the shipped config; generate it
    # rather than enumerating guaranteed-rejected noise.
    actions.append(("interrupt", "p1_channel", "player-3"))
    for player in ACTOR_IDS:
        actions.append(("kill", player))
    return actions


def _light_run(actions: List[Action], config: Any) -> Optional[_SearchState]:
    """Execute a prefix on a throwaway real coordinator to read its state."""

    from ..clock import Clock
    from ..coordinator import PhaseCoordinator
    from ..events import EventBus

    bus = EventBus()
    clock = Clock()
    coordinator = PhaseCoordinator(config, bus, clock)
    try:
        for action in actions:
            kind = action[0]
            if kind == "start":
                coordinator.start_encounter()
            elif kind == "wait":
                coordinator.tick(clock.now + action[1])
            elif kind == "boss_damage":
                coordinator.apply_damage(config.boss.boss_id, action[1], action[2])
            elif kind == "target_damage":
                coordinator.apply_damage(action[1], action[2], action[3])
            elif kind == "destroy":
                coordinator.destroy_target(action[1], source=action[2])
            elif kind == "kill":
                coordinator.player_dies(action[1])
            elif kind == "interrupt":
                coordinator.interrupt_mechanic(action[1], action[2])
            elif kind == "inject_stale":
                return None
    except Exception:
        return None
    claims = len(coordinator.resources.active_claims())
    return _SearchState(
        started=coordinator.generation > 0,
        status=coordinator.status,
        phase_index=coordinator.phase_index,
        hp_bucket=_hp_bucket(coordinator.boss_hp, config.boss.max_hp),
        wardstone_dead=coordinator.target_hp.get(WARDSTONE, 0.0) == 0.0,
        brazier_dead=coordinator.target_hp.get(BRAZIER, 0.0) == 0.0,
        players_alive=len(coordinator.players),
        occupancy_count=claims,
        generation=coordinator.generation,
    )


def enumerate_scenarios(
    max_depth: int = MAX_DEPTH, max_nodes: int = MAX_NODES
) -> List[Scenario]:
    """Depth-first enumeration of short legal sequences, pruned by signature."""

    config = build_encounter_config()
    seen: Set[Tuple[Any, ...]] = set()
    found: List[Scenario] = []
    nodes = 0

    def visit(prefix: List[Action], depth: int) -> None:
        nonlocal nodes
        if nodes >= max_nodes or depth > max_depth:
            return
        state = _light_run(prefix, config)
        if state is None:
            return
        signature = (depth, *state.signature())
        if signature in seen:
            return
        seen.add(signature)
        nodes += 1
        if depth > 0 and state.status in ("wipe", "victory"):
            found.append(
                Scenario(
                    f"enum_{len(found):03d}",
                    f"Enumerated terminal sequence of length {depth}.",
                    list(prefix),
                    origin="enumerated",
                    features=features_for_prefix(prefix, state),
                )
            )
            return
        if depth == max_depth:
            found.append(
                Scenario(
                    f"enum_{len(found):03d}",
                    f"Enumerated depth-bound sequence of length {depth}.",
                    list(prefix),
                    origin="enumerated",
                    features=features_for_prefix(prefix, state),
                )
            )
            return
        for action in _legal_actions(state):
            visit([*prefix, action], depth + 1)

    visit([], 0)
    return found


def features_for_prefix(prefix: List[Action], state: _SearchState) -> List[str]:
    features: List[str] = []
    kinds = [a[0] for a in prefix]
    if any(a[0] == "boss_damage" and a[1] >= 400 for a in prefix):
        features.append("big_damage_multi_threshold")
    if any(a[0] == "boss_damage" and a[1] == 200 for a in prefix):
        features.append("overlapping_threshold_priority")
    if "destroy" in kinds and state.phase_index >= 1:
        features.append("target_gated_phase")
    if state.occupancy_count >= 1:
        features.append("mutex_contention_block")
    if any(a[0] == "wait" and a[1] >= 8.0 for a in prefix):
        features.append("periodic_timer")
    if "interrupt" in kinds:
        features.append("interrupt_channel")
    if state.generation >= 2:
        features.append("multiple_wipe_restart")
    if state.phase_index >= 2:
        features.append("cross_phase_carry")
    if state.status in ("wipe", "victory"):
        features.append("cross_phase_cancel")
    return features


def generate_scenarios() -> List[Scenario]:
    """All handcrafted contracts followed by the bounded enumeration."""

    scenarios = handcrafted_scenarios()
    for scenario in enumerate_scenarios():
        scenario.scenario_id = f"enum_{len(scenarios):03d}"
        scenarios.append(scenario)
    return scenarios


# --------------------------------------------------------------------------- #
# Coverage ledger
# --------------------------------------------------------------------------- #

@dataclass
class CoverageLedger:
    feature_hits: Dict[str, int] = field(default_factory=dict)
    feature_scenarios: Dict[str, List[str]] = field(default_factory=dict)
    combinations_hit: Set[Tuple[Any, ...]] = field(default_factory=set)
    combinations_uncovered: List[Tuple[Any, ...]] = field(default_factory=list)
    enumeration_bounds: Dict[str, int] = field(default_factory=dict)
    total_combinations: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enumeration_bounds": self.enumeration_bounds,
            "features": {
                feature: {
                    "hits": self.feature_hits.get(feature, 0),
                    "scenarios": sorted(self.feature_scenarios.get(feature, [])),
                    "covered": self.feature_hits.get(feature, 0) > 0,
                }
                for feature in FEATURES
            },
            "state_combinations": {
                "possible": self.total_combinations,
                "exercised": len(self.combinations_hit),
                "uncovered_count": len(self.combinations_uncovered),
                "uncovered": [list(combo) for combo in self.combinations_uncovered],
            },
        }


def _occupancy_bucket(count: int) -> str:
    if count == 0:
        return "empty"
    if count == 1:
        return "single"
    if count == 2:
        return "contended"
    return "multi"


def coverage_ledger(scenarios: List[Scenario]) -> CoverageLedger:
    """Re-run every scenario and record the (state, feature) combinations hit."""

    config = build_encounter_config()
    ledger = CoverageLedger(
        enumeration_bounds={"max_depth": MAX_DEPTH, "max_nodes": MAX_NODES}
    )
    for scenario in scenarios:
        for feature in scenario.features:
            ledger.feature_hits[feature] = ledger.feature_hits.get(feature, 0) + 1
            ledger.feature_scenarios.setdefault(feature, []).append(
                scenario.scenario_id
            )
        state = _light_run(scenario.actions, config)
        if state is None or not scenario.features:
            continue
        for feature in scenario.features:
            ledger.combinations_hit.add(
                (
                    _phase_bucket(state),
                    state.hp_bucket,
                    _occupancy_bucket(state.occupancy_count),
                    _players_bucket(state.players_alive),
                    "round2plus" if state.generation >= 2 else "round1",
                    feature,
                )
            )

    feasible_set = set()
    for phase, hps, occupancies, players, rounds in FEASIBLE_RULES:
        for hp in hps:
            for occupancy in occupancies:
                for players_bucket in players:
                    for round_ in rounds:
                        for feature in FEATURES:
                            feasible_set.add(
                                (phase, hp, occupancy, players_bucket, round_, feature)
                            )
    # Only hits that fall inside the declared feasible space count toward
    # coverage; out-of-space tuples (e.g. the unclassified placeholder) are not
    # silently subtracted from the uncovered total.
    ledger.combinations_hit = {
        combo for combo in ledger.combinations_hit if combo in feasible_set
    }
    ledger.total_combinations = len(feasible_set)
    ledger.combinations_uncovered = [
        combo for combo in sorted(feasible_set) if combo not in ledger.combinations_hit
    ]
    return ledger


def _phase_bucket(state: _SearchState) -> str:
    if state.status in ("wipe", "victory"):
        return "terminal"
    if state.phase_index < 0:
        return "idle"
    return ("p1", "p2", "p3")[min(state.phase_index, 2)]


def _players_bucket(alive: int) -> str:
    if alive == 0:
        return "none"
    if alive >= 3:
        return "all"
    return "partial"
