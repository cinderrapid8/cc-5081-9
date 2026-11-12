"""The presentation layer must only react to coordinator events."""

import json
import os
import tempfile
import unittest

from harness import Harness


class PresentationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.h.start()

    def test_view_changes_only_through_events(self) -> None:
        renderer = self.h.renderer
        # The renderer holds no coordinator reference by construction.
        self.assertFalse(hasattr(renderer, "coordinator"))
        # start_encounter immediately enters P1, driven entirely by the event.
        self.assertEqual(renderer.view.phase, "p1")

        self.h.coordinator.apply_damage("boss-overlord", 200, "player-2")
        self.assertEqual(renderer.view.boss_hp, 800.0)
        self.assertIn("p1_seal", renderer.view.active_mechanics)
        self.assertEqual(renderer.view.claims["object:brazier"], "p1_seal")

        self.h.coordinator.apply_damage("boss-overlord", 250, "player-2")
        self.h.coordinator.destroy_target("wardstone", "player-1")
        self.assertEqual(renderer.view.phase, "p2")
        # A carried-over mechanic remains visible and still shows its claims.
        self.assertIn("p1_seal", renderer.view.active_mechanics)

    def test_every_emitted_event_type_is_rendered(self) -> None:
        # Exercise a broad surface of events, then require zero unhandled ones.
        self.h.coordinator.tick(8.0)
        self.h.coordinator.interrupt_mechanic("p1_channel", "player-3")
        self.h.coordinator.interrupt_mechanic("p1_seal", "player-3")  # rejected
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")
        self.h.coordinator.destroy_target("wardstone", "player-1")
        self.h.coordinator.destroy_target("brazier", "player-2")
        for player in ("player-1", "player-2", "player-3"):
            self.h.coordinator.player_dies(player)
        self.assertEqual(self.h.renderer.unrecognized_events, [])

    def test_transcript_mentions_every_phase_and_final_status(self) -> None:
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")
        self.h.coordinator.destroy_target("wardstone", "player-1")
        self.h.coordinator.apply_damage("boss-overlord", 260, "player-2")
        text = self.h.renderer.render()
        self.assertIn("PHASE 1 ENTER", text)
        self.assertIn("PHASE 2 ENTER", text)
        self.assertIn("PHASE 3 ENTER", text)
        self.assertIn("VICTORY", text)
        self.assertIn("ROUND CLEAN  residual claims=[]", text)

    def test_run_record_serializes_and_round_trips(self) -> None:
        self.h.coordinator.tick(8.0)
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")
        self.h.coordinator.destroy_target("wardstone", "player-1")
        record = self.h.recorder.current
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "record.json")
            record.save_json(path)
            with open(path, encoding="utf-8") as handle:
                blob = json.load(handle)

        self.assertEqual(blob["encounter_id"], "overlord-rehearsal")
        self.assertEqual(blob["event_count"], len(record.events))
        phase_ids = [entry["phase_id"] for entry in blob["phase_timeline"]]
        self.assertEqual(phase_ids, ["p1", "p2"])
        # Sequence numbers are monotonic across the whole record.
        seqs = [event["seq"] for event in blob["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))

    def test_recorder_creates_one_record_per_generation(self) -> None:
        self.h.coordinator.player_dies("player-1")
        self.h.coordinator.player_dies("player-2")
        self.h.coordinator.player_dies("player-3")
        self.h.coordinator.reset_after_wipe()
        self.h.start()
        self.assertEqual(len(self.h.recorder.records), 2)
        self.assertEqual(self.h.recorder.records[0].final_status, "wipe")
        self.assertEqual(self.h.recorder.records[1].final_status, "idle")


if __name__ == "__main__":
    unittest.main()
