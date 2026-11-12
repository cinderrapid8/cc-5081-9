"""Wipe/reset hygiene and late callbacks from a dead generation."""

import unittest

from harness import Harness


class ResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.h.start()

    def test_wipe_cancels_all_timers_and_frees_every_claim(self) -> None:
        self.h.coordinator.tick(8.0)  # channel + pulse active, pulse re-armed
        self.h.coordinator.apply_damage("boss-overlord", 200, "player-2")  # seal active
        self.assertFalse(self.h.coordinator.resources.is_empty())

        for player in ("player-1", "player-2", "player-3"):
            self.h.coordinator.player_dies(player)

        self.assertEqual(self.h.coordinator.status, "wipe")
        self.h.assert_clean_teardown()
        # Every pending timer from the dead generation was explicitly cancelled.
        self.assertTrue(self.h.payloads("timer_cancelled"))
        self.assertEqual(
            self.h.latest("round_clean")["claims"], []
        )
        # Nothing left in the clock heap either.
        self.assertEqual(self.h.clock.pending(), [])

    def test_reset_then_restart_has_no_residual_occupancy(self) -> None:
        self.h.coordinator.tick(8.0)
        for player in ("player-1", "player-2", "player-3"):
            self.h.coordinator.player_dies(player)
        self.h.coordinator.reset_after_wipe()

        self.assertEqual(self.h.coordinator.status, "idle")
        self.assertTrue(self.h.coordinator.resources.is_empty())
        self.assertEqual(self.h.clock.now, 0.0)
        self.assertEqual(self.h.clock.pending(), [])

        # A brand-new round starts clean and independently.
        self.h.start()
        self.assertEqual(self.h.coordinator.generation, 2)
        self.assertEqual(self.h.coordinator.current_phase.phase_id, "p1")
        self.assertEqual(self.h.coordinator.boss_hp, 1000.0)
        self.assertEqual(self.h.coordinator.target_hp["wardstone"], 200.0)
        self.assertTrue(self.h.coordinator.resources.is_empty())
        # Fired-threshold memory must not leak across rounds.
        self.h.coordinator.apply_damage("boss-overlord", 200, "player-2")
        self.assertIn(
            "p1_seal",
            [p["mechanic_id"] for p in self.h.payloads("mechanic_start")],
        )

    def test_late_callback_from_old_generation_is_dropped_not_executed(self) -> None:
        # A callback from the wiped generation that only reaches the front of
        # the queue *after* a new round has begun.
        import heapq

        for player in ("player-1", "player-2", "player-3"):
            self.h.coordinator.player_dies(player)
        self.h.coordinator.reset_after_wipe()
        self.assertEqual(self.h.clock.pending(), [])

        # Inject a late gen-1 timer into the otherwise clean clock.
        stale = self.h.clock.schedule(
            delay=9.0,
            generation=1,
            key=("phase_timer", "p1", "p1_channel"),
            callback=lambda handle: None,
        )
        self.assertEqual(stale.generation, 1)

        self.h.start()  # generation 2; a fresh channel timer is armed at t=5
        self.h.coordinator.tick(10.0)

        dropped = self.h.payloads("timer_dropped_stale")
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["timer_generation"], 1)
        self.assertEqual(dropped[0]["current_generation"], 2)

        # The new round's own channel still fires exactly once at t=5; the
        # stale copy never starts a second, generation-1 instance.
        channel_starts = [
            p
            for p in self.h.payloads("mechanic_start")
            if p["mechanic_id"] == "p1_channel"
        ]
        self.assertEqual(len(channel_starts), 1)
        self.assertEqual(channel_starts[0]["generation"], 2)

    def test_actions_after_teardown_are_rejected(self) -> None:
        for player in ("player-1", "player-2", "player-3"):
            self.h.coordinator.player_dies(player)
        with self.assertRaises(Exception):
            self.h.coordinator.apply_damage("boss-overlord", 10, "player-2")
        with self.assertRaises(Exception):
            self.h.coordinator.tick(1.0)


if __name__ == "__main__":
    unittest.main()
