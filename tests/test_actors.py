"""Test-character actions: interrupts, revives, timer and periodic mechanics."""

import unittest

from harness import Harness


class ActorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.h.start()

    def test_interrupt_stops_channel_and_releases_claim(self) -> None:
        self.h.coordinator.tick(5.0)
        ok = self.h.coordinator.interrupt_mechanic("p1_channel", "player-3")
        self.assertTrue(ok)
        channel = self.h.coordinator.mechanics["p1_channel"]
        self.assertEqual(channel.status.value, "cancelled")
        self.assertEqual(channel.detail, "interrupted")
        self.assertIsNone(
            self.h.coordinator.resources.holder_of("position:north_dais")
        )

    def test_interrupting_non_interruptible_mechanic_is_rejected(self) -> None:
        self.h.coordinator.apply_damage("boss-overlord", 200, "player-2")  # p1_seal
        ok = self.h.coordinator.interrupt_mechanic("p1_seal", "player-3")
        self.assertFalse(ok)
        self.assertEqual(self.h.latest("interrupt_rejected")["reason"], "not_interruptible")
        # Still running and still holding its claims.
        self.assertTrue(self.h.coordinator.mechanics["p1_seal"].active)

    def test_interrupting_inactive_mechanic_is_rejected(self) -> None:
        ok = self.h.coordinator.interrupt_mechanic("p1_channel", "player-3")
        self.assertFalse(ok)
        self.assertEqual(self.h.latest("interrupt_rejected")["reason"], "not_active")

    def test_periodic_mechanic_rearms_until_phase_ends(self) -> None:
        # Incremental pumps let each 2s pulse finish before its 8s re-arm fires,
        # so the re-armed instances are not blocked by their own resource.
        for t in (8.0, 16.0, 24.0):
            self.h.coordinator.tick(t)
        starts = [
            e for e in self.h.events
            if e.type == "mechanic_start" and e.payload["mechanic_id"] == "p1_pulse"
        ]
        self.assertEqual(len(starts), 3)
        # Ending the phase cancels the periodic timer and the active pulse.
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")
        self.h.coordinator.destroy_target("wardstone", "player-1")
        self.assertEqual(
            self.h.coordinator.clock.cancel_key(
                ("phase_timer", "p1", "p1_pulse")
            ),
            0,
        )
        self.assertNotIn(
            "p1_pulse",
            [m.mechanic_id for m in self.h.coordinator.active_mechanics],
        )

    def test_mechanic_ending_naturally_releases_its_resources(self) -> None:
        self.h.coordinator.tick(5.0)  # channel starts, duration 10
        self.h.coordinator.tick(15.0)
        self.assertIsNone(
            self.h.coordinator.resources.holder_of("position:north_dais")
        )
        self.assertEqual(
            self.h.coordinator.mechanics["p1_channel"].status.value, "completed"
        )

    def test_player_revive_prevents_and_recovers_from_wipe(self) -> None:
        self.h.coordinator.player_dies("player-1")
        self.h.coordinator.player_dies("player-2")
        self.h.coordinator.player_revived("player-1")
        self.h.coordinator.player_dies("player-3")
        # player-1 is back, so the encounter survives.
        self.assertEqual(self.h.coordinator.status, "active")
        self.assertNotIn("wipe", self.h.types())

    def test_unknown_player_cannot_act(self) -> None:
        with self.assertRaises(Exception):
            self.h.coordinator.interrupt_mechanic("p1_channel", "player-9")


if __name__ == "__main__":
    unittest.main()
