"""Trace vocabulary shared by the reference model and the real runner.

Both sides produce snapshots and counters with identical key names, so a
property failure is always a plain data diff rather than a comparison between
two object models.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Mechanic-level counters keyed by mechanic id.
MECHANIC_COUNTERS = (
    "mechanic_start",
    "mechanic_end",
    "mechanic_blocked",
    "mechanic_revoked",
    "mechanic_deferred",
    "interrupt_rejected",
)

# Clock-level counters, keyed by plain integers.
SCALAR_COUNTERS = ("timer_dropped_stale", "timer_cancelled")

SNAPSHOT_KEYS = (
    "time",
    "generation",
    "status",
    "phase_id",
    "phase_index",
    "boss_hp",
    "target_hp",
    "players",
    "occupancy",
    "active_mechanics",
)


def empty_counters() -> Dict[str, Any]:
    counters: Dict[str, Any] = {name: {} for name in MECHANIC_COUNTERS}
    counters.update({name: 0 for name in SCALAR_COUNTERS})
    return counters


def bump(counters: Dict[str, Any], name: str, key: str) -> None:
    counters[name][key] = counters[name].get(key, 0) + 1


def normalize_snapshot(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Order every collection so equality is structural, not incidental."""

    return {
        "time": raw["time"],
        "generation": raw["generation"],
        "status": raw["status"],
        "phase_id": raw["phase_id"],
        "phase_index": raw["phase_index"],
        "boss_hp": raw["boss_hp"],
        "target_hp": dict(sorted(raw["target_hp"].items())),
        "players": sorted(raw["players"]),
        "occupancy": sorted(
            ({"resource": r, "owner": o} for r, o in raw["occupancy"]),
            key=lambda item: item["resource"],
        ),
        "active_mechanics": sorted(raw["active_mechanics"]),
    }


def diff_counters(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return every counter bucket whose dispatch counts disagree."""

    diffs: List[Dict[str, Any]] = []
    for name in (*MECHANIC_COUNTERS, *SCALAR_COUNTERS):
        if expected.get(name) != actual.get(name):
            diffs.append(
                {"counter": name, "expected": expected.get(name), "actual": actual.get(name)}
            )
    return diffs


def diff_snapshot(expected: Dict[str, Any], actual: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Field-level snapshot diff, including per-resource occupancy detail."""

    diffs: List[Dict[str, Any]] = []
    for key in SNAPSHOT_KEYS:
        if expected.get(key) != actual.get(key):
            diffs.append(
                {"field": key, "expected": expected.get(key), "actual": actual.get(key)}
            )
    return diffs
