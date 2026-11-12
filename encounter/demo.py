"""A complete, scripted rehearsal of the encounter.

Run with ``python -m encounter.demo``.  Two rounds are played on one
coordinator:

1. a full clear through all three phases, exercising overlapping thresholds,
   resource contention, interruption, target-gated entry and cross-phase
   policies;
2. a wipe followed by a reset and second clear, proving late timers are
   dropped as stale and the new round starts with zero residual claims.

The rendered transcript and machine-readable run records are written under
``run_records/``.
"""

from __future__ import annotations

import os
from typing import List

from .presentation import EventRenderer
from .recorder import RunRecord
from .scenario import BRAZIER, WARDSTONE
from .simulator import Simulator

RECORD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "run_records")


def full_clear_actions(sim: Simulator) -> List:
    return [
        sim.start(),
        sim.wait(3),                                   # t=3, nothing due yet
        sim.boss_damage(200),                          # 1000 -> 800: shared 80% boundary
        sim.wait(3),                                   # t=6: channel fires at 5, interrupt at 6
        sim.interrupt("player-3", "p1_channel"),
        sim.interrupt("player-1", "p1_surge"),         # never started -> rejected
        sim.wait(3),                                   # t=9: first pulse at 8
        sim.boss_damage(100),                          # 800 -> 700
        sim.boss_damage(100),                          # 700 -> 600: last_wrath fires, P2 waits on wardstone
        sim.destroy("player-1", WARDSTONE),            # target gate satisfied -> enter P2
        sim.damage("player-2", BRAZIER, 60),           # soften the brazier early
        sim.wait(4),                                   # P2 t+4: soul chain starts
        sim.boss_damage(150),                          # 600 -> 450: brazier burn boundary
        sim.wait(2),                                   # let it commit
        sim.destroy("player-2", BRAZIER),              # required target gone mid-mechanic
        sim.wait(5),                                   # P2 t+11: cataclysm at +10
        sim.boss_damage(100),                          # 450 -> 350
        sim.boss_damage(50),                           # 350 -> 300 -> P3 entry, victory
    ]


def wipe_then_clear_actions(sim: Simulator) -> List:
    return [
        sim.start(),
        sim.boss_damage(200),                          # 80% mechanics commit
        sim.wait(2),
        sim.kill_player("player-1"),
        sim.kill_player("player-2"),
        sim.kill_player("player-3"),                   # wipe: all timers cancelled
        sim.reset(),
        # Second round on the same coordinator instance.
        sim.start(),
        sim.boss_damage(450),                          # straight through the 80%/60% boundaries
        sim.destroy("player-1", WARDSTONE),            # unlock P2 immediately
        sim.wait(1),
        sim.boss_damage(260),                          # -> 290, straight into P3 victory
    ]


def _summarize(record: RunRecord) -> str:
    types = {}
    for event in record.events:
        types[event.type] = types.get(event.type, 0) + 1
    timeline = " -> ".join(
        f"{entry['phase_id']}@t={entry['time']}" for entry in record.phase_timeline()
    )
    return (
        f"generation {record.generation}: status={record.final_status}, "
        f"{len(record.events)} events\n  phases: {timeline}\n  counts: {types}"
    )


def main() -> None:
    os.makedirs(RECORD_DIR, exist_ok=True)
    sim = Simulator()

    renderer = EventRenderer()
    sim.bus.subscribe(renderer)

    print("=" * 78)
    print("ROUND A - FULL CLEAR REHEARSAL")
    print("=" * 78)
    sim.run(full_clear_actions(sim))
    print(renderer.render())
    print()
    print(_summarize(sim.recorder.records[0]))
    sim.recorder.records[0].save_json(os.path.join(RECORD_DIR, "round_a_full_clear.json"))

    print()
    print("=" * 78)
    print("ROUND B - WIPE, RESET AND SECOND CLEAR (stale-timer proof)")
    print("=" * 78)
    before = len(renderer.transcript)
    sim.run(wipe_then_clear_actions(sim))
    print("\n".join(renderer.transcript[before:]))
    print()
    for record in sim.recorder.records[1:]:
        print(_summarize(record))
    sim.recorder.records[1].save_json(os.path.join(RECORD_DIR, "round_b_wipe.json"))
    sim.recorder.records[2].save_json(os.path.join(RECORD_DIR, "round_b_reclear.json"))

    print()
    print(f"records written to {RECORD_DIR}")


if __name__ == "__main__":
    main()
