"""Canonical action alphabet shared by the generator, runner and oracle.

An action is a small tuple whose first element names the operation.  Keeping the
model declarative means the same objects drive three different consumers:

* the bounded enumerator in :mod:`encounter.acceptance.generator`;
* the real runner in :mod:`encounter.acceptance.runner`;
* the independent reference model in :mod:`encounter.acceptance.oracle`.

The tuples round-trip through plain JSON so every generated scenario and every
failure replay can be opened by the graphical client without Python-specific
types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

# Canonical action tuples.
#   ("start",)                       start (or restart after a reset) the fight
#   ("wait", seconds)                pump the virtual clock
#   ("boss_damage", amount, actor)   strike the boss
#   ("target_damage", target, amount, actor)
#   ("destroy", target, actor)       scripted target destruction
#   ("kill", player)                 a player slot dies
#   ("interrupt", mechanic, player)  attempt to break a channel
#   ("inject_stale",)                enqueue a callback belonging to the wiped
#                                    generation so its rejection can be observed
Action = Tuple[Any, ...]

ACTOR_IDS = ("player-1", "player-2", "player-3")

# Damage amounts chosen so sequences hit, straddle and overshoot every HP gate
# (boundaries at 80%, 60%, 45%, 30%, 20% of the 1000 HP boss).
BOSS_DAMAGES = (10, 50, 150, 200, 250, 350, 400, 560, 1000)
TARGET_DAMAGES = (30, 80, 200)
WAITS = (1.0, 3.0, 5.0, 10.0)

# Keys understood by the JSON form of an action.  The order doubles as the
# positional order of the tuple.
_ACTION_FIELDS: Dict[str, Tuple[str, ...]] = {
    "start": (),
    "wait": ("seconds",),
    "boss_damage": ("amount", "actor"),
    "target_damage": ("target", "amount", "actor"),
    "destroy": ("target", "actor"),
    "kill": ("player",),
    "interrupt": ("mechanic", "player"),
    "inject_stale": (),
}


def action_to_dict(action: Action) -> Dict[str, Any]:
    kind = action[0]
    if kind not in _ACTION_FIELDS:
        raise ValueError(f"unknown action kind {kind!r}")
    fields = _ACTION_FIELDS[kind]
    if len(action) - 1 != len(fields):
        raise ValueError(f"action {action!r} does not match its schema")
    data: Dict[str, Any] = {"kind": kind}
    for name, value in zip(fields, action[1:]):
        data[name] = value
    return data


def action_from_dict(data: Dict[str, Any]) -> Action:
    kind = data["kind"]
    if kind not in _ACTION_FIELDS:
        raise ValueError(f"unknown action kind {kind!r}")
    return (kind, *(data[name] for name in _ACTION_FIELDS[kind]))


@dataclass
class Scenario:
    """A named, replayable event sequence plus why it exists."""

    scenario_id: str
    description: str
    actions: List[Action] = field(default_factory=list)
    # Provenance: "handcrafted" for the edge-case contracts, "enumerated" for
    # bounded model output, "mutation" for fault-injection probes.
    origin: str = "handcrafted"
    # Which acceptance features the generator intended this sequence to cover.
    features: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "description": self.description,
            "origin": self.origin,
            "features": list(self.features),
            "actions": [action_to_dict(a) for a in self.actions],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Scenario":
        return cls(
            scenario_id=data["scenario_id"],
            description=data.get("description", ""),
            actions=[action_from_dict(a) for a in data["actions"]],
            origin=data.get("origin", "handcrafted"),
            features=list(data.get("features", [])),
        )
