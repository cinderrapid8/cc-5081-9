"""End-to-end rehearsal through the scripted simulator."""

import unittest

from encounter.demo import full_clear_actions, wipe_then_clear_actions
from encounter.presentation import EventRenderer
from encounter.simulator import Simulator


class RehearsalTests(unittest.TestCase):
    def test_full_clear_visits_every_phase_and_wins(self) -> None:
        sim = Simulator()
        renderer = EventRenderer()
        sim.bus.subscribe(renderer)
        sim.run(full_clear_actions(sim))

        self.assertEqual(sim.coordinator.status, "victory")
        phase_ids = [p["phase_id"] for p in sim.recorder.current.phase_timeline()]
        self.assertEqual(phase_ids, ["p1", "p2", "p3"])

        # Contention was observed, not silently queued.
        blocked = [
            e.payload["mechanic_id"]
            for e in sim.recorder.current.events_of("mechanic_blocked")
        ]
        self.assertIn("p1_surge", blocked)
        # Both cross-phase policies are visibly exercised.
        self.assertTrue(sim.recorder.current.events_of("mechanic_carried_over"))
        cancelled = [
            e.payload for e in sim.recorder.current.events_of("mechanic_end")
            if e.payload["reason"] == "phase_change"
        ]
        self.assertTrue(cancelled)
        # Interruption path.
        self.assertTrue(sim.recorder.current.events_of("mechanic_end"))
        interrupts = [
            e.payload for e in sim.recorder.current.events_of("mechanic_end")
            if e.payload["reason"] == "interrupted"
        ]
        self.assertTrue(interrupts)
        # Nothing left occupied, renderer agrees, no event went unrendered.
        self.assertTrue(sim.coordinator.resources.is_empty())
        self.assertEqual(renderer.view.claims, {})
        self.assertEqual(renderer.unrecognized_events, [])

    def test_wipe_reset_and_reclear_on_one_coordinator(self) -> None:
        sim = Simulator()
        sim.run(wipe_then_clear_actions(sim))

        self.assertEqual(len(sim.recorder.records), 2)
        self.assertEqual(sim.recorder.records[0].final_status, "wipe")
        self.assertEqual(sim.recorder.records[1].final_status, "victory")
        self.assertEqual(sim.coordinator.generation, 2)
        # The second round's events carry the new generation.
        for event in sim.recorder.records[1].events:
            if "generation" in event.payload:
                self.assertEqual(event.payload["generation"], sim.coordinator.generation)
        self.assertTrue(sim.coordinator.resources.is_empty())

    def test_event_sequence_is_total_order(self) -> None:
        sim = Simulator()
        sim.run(full_clear_actions(sim))
        seqs = [e.seq for e in sim.recorder.current.events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))


if __name__ == "__main__":
    unittest.main()
