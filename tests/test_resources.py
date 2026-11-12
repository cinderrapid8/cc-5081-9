"""Resource contention and targets destroyed before mechanics consume them."""

import unittest

from harness import Harness


class ContentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.h.start()

    def test_failed_claim_leaves_no_partial_occupancy(self) -> None:
        # p1_seal takes object:brazier + position:center atomically first.
        self.h.coordinator.apply_damage("boss-overlord", 200, "player-2")
        before = {c.resource: c.owner for c in self.h.coordinator.resources.active_claims()}
        # p1_surge fails on object:brazier; nothing it wanted may appear held.
        blocked = self.h.latest("mechanic_blocked")
        self.assertEqual(blocked["mechanic_id"], "p1_surge")
        after = {c.resource: c.owner for c in self.h.coordinator.resources.active_claims()}
        self.assertEqual(before, after)

    def test_released_resource_can_be_claimed_again(self) -> None:
        self.h.coordinator.tick(5.0)  # channel holds north_dais for 10s
        ok = self.h.coordinator.interrupt_mechanic("p1_channel", "player-1")
        self.assertTrue(ok)
        self.assertIsNone(
            self.h.coordinator.resources.holder_of("position:north_dais")
        )

    def test_target_destroyed_early_revokes_dependent_mechanic(self) -> None:
        # Drive to P2 first, then destroy the brazier before the 45% boundary.
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")  # 550
        self.h.coordinator.destroy_target("wardstone", "player-1")
        self.h.coordinator.destroy_target("brazier", "player-2")
        # Crossing 45% now: p2_burn requires a living brazier.
        self.h.coordinator.apply_damage("boss-overlord", 100, "player-2")  # 450
        revoked = [p["mechanic_id"] for p in self.h.payloads("mechanic_revoked")]
        self.assertIn("p2_burn", revoked)
        self.assertEqual(
            self.h.coordinator.mechanics["p2_burn"].detail, "target_gone"
        )

    def test_target_destroyed_mid_mechanic_cancels_and_releases(self) -> None:
        # Enter P2 at 550 HP; the 45% boundary (450 HP) has not been crossed,
        # so p2_burn stays deferred and object:brazier is held only by the
        # carried-over p1_seal until its 12s duration ends.
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")  # 550
        self.h.coordinator.destroy_target("wardstone", "player-1")        # P2
        self.h.coordinator.tick(13.0)  # p1_seal finishes, brazier freed
        self.h.coordinator.apply_damage("boss-overlord", 100, "player-2")  # 450
        self.assertEqual(
            self.h.coordinator.resources.holder_of("object:brazier"), "p2_burn"
        )
        self.h.coordinator.destroy_target("brazier", "player-2")
        self.assertIsNone(
            self.h.coordinator.resources.holder_of("object:brazier")
        )
        end = [
            p for p in self.h.payloads("mechanic_end") if p["mechanic_id"] == "p2_burn"
        ][0]
        self.assertEqual(end["reason"], "target_gone")
        self.assertEqual(end["outcome"], "cancelled")

    def test_destroying_unknown_target_is_an_error(self) -> None:
        with self.assertRaises(Exception):
            self.h.coordinator.destroy_target("does-not-exist", "player-1")


if __name__ == "__main__":
    unittest.main()
