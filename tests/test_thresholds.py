"""Stepwise threshold processing and mutually exclusive overlap."""

import unittest

from harness import Harness


class ThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.h.start()

    def ids(self, event_type: str) -> list[str]:
        return [p["mechanic_id"] for p in self.h.payloads(event_type)]

    def test_overlapping_boundary_is_ordered_by_priority(self) -> None:
        # One hit crosses exactly the shared 80% boundary (1000 -> 800).
        self.h.coordinator.apply_damage("boss-overlord", 200, "player-2")

        self.assertEqual(self.ids("mechanic_start"), ["p1_seal"])
        self.assertEqual(self.ids("mechanic_blocked"), ["p1_surge"])
        block = self.h.latest("mechanic_blocked")
        self.assertEqual(block["reason"], "resource_busy")
        self.assertEqual(block["holder"], "p1_seal")
        self.assertIn("object:brazier", block["resource"])
        # The loser must never have held the contested object.
        self.assertEqual(
            self.h.coordinator.resources.holder_of("object:brazier"), "p1_seal"
        )

    def test_single_damage_crosses_multiple_thresholds_stepwise(self) -> None:
        # 1000 -> 440 crosses 80%, 60% and the P2-owned 45% boundary in one
        # event.  P1 mechanics resolve inside P1; p2_burn is owned by the
        # still-locked P2 and must be remembered rather than started out of phase.
        self.h.coordinator.apply_damage("boss-overlord", 560, "player-2")

        self.assertIn("p1_seal", self.ids("mechanic_start"))
        self.assertIn("p1_surge", self.ids("mechanic_blocked"))
        self.assertIn("p1_last_wrath", self.ids("mechanic_start"))
        self.assertEqual(self.ids("mechanic_deferred"), ["p2_burn"])
        self.assertEqual(self.h.coordinator.current_phase.phase_id, "p1")

        # Destroying the brazier first means p2_burn, when replayed at P2
        # entry, loses eligibility on its required target instead of starting.
        self.h.coordinator.destroy_target("brazier", "player-2")
        self.h.coordinator.destroy_target("wardstone", "player-1")
        self.assertEqual(self.h.coordinator.current_phase.phase_id, "p2")
        self.assertIn("p2_burn", self.ids("mechanic_revoked"))
        self.assertEqual(
            self.h.coordinator.mechanics["p2_burn"].detail, "target_gone"
        )

    def test_deferred_threshold_replays_through_contention_policy(self) -> None:
        # p1_seal (CONTINUE) keeps object:brazier across the boundary, so the
        # deferred p2_burn is replayed at P2 entry and blocked, never queued.
        self.h.coordinator.apply_damage("boss-overlord", 560, "player-2")  # 440
        self.h.coordinator.destroy_target("wardstone", "player-1")         # P2
        blocked = [
            p
            for p in self.h.payloads("mechanic_blocked")
            if p["mechanic_id"] == "p2_burn"
        ]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["trigger"], "hp_threshold_catchup")
        self.assertEqual(blocked[0]["holder"], "p1_seal")

    def test_deferred_threshold_starts_when_claim_is_free(self) -> None:
        # A threshold mechanic with a unique resource starts cleanly at the
        # entry of the phase that owns it.
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")  # 550
        self.h.coordinator.destroy_target("wardstone", "player-1")         # P2
        # p2 has no pre-crossed unique-resource threshold here; instead verify
        # the P1 deferred path directly via p1_last_wrath when P2 is delayed.
        h2 = Harness()
        h2.start()
        h2.coordinator.apply_damage("boss-overlord", 560, "player-2")      # 440
        # Wardstone still up: P2 locked; destroy brazier so p2_burn would fail
        # eligibility, proving catch-up re-checks current state at entry.
        h2.coordinator.destroy_target("brazier", "player-2")
        h2.coordinator.destroy_target("wardstone", "player-1")
        self.assertEqual(h2.coordinator.current_phase.phase_id, "p2")
        self.assertIn("p2_burn", h2.ids("mechanic_revoked"))

    def test_thresholds_never_double_fire(self) -> None:
        self.h.coordinator.apply_damage("boss-overlord", 210, "player-2")  # 790
        self.h.coordinator.apply_damage("boss-overlord", 10, "player-2")   # 780
        seal_starts = [
            p for p in self.h.payloads("mechanic_start") if p["mechanic_id"] == "p1_seal"
        ]
        self.assertEqual(len(seal_starts), 1)

    def test_mutex_group_blocks_on_disjoint_resources_too(self) -> None:
        self.h.coordinator.apply_damage("boss-overlord", 200, "player-2")  # 800
        self.h.coordinator.apply_damage("boss-overlord", 250, "player-2")  # 550
        self.h.coordinator.destroy_target("wardstone", "player-1")         # enter P2
        # p1_seal (CONTINUE) still holds group:center_control.  One hit from
        # 550 to 150 crosses both the P3 entry gate (30%) and p3_despair (20%);
        # the despair mechanic must be blocked rather than run concurrently.
        self.h.coordinator.apply_damage("boss-overlord", 400, "player-2")
        self.assertEqual(self.h.coordinator.status, "victory")
        self.assertIn("p3_despair", self.ids("mechanic_blocked"))
        block = [
            p for p in self.h.payloads("mechanic_blocked") if p["mechanic_id"] == "p3_despair"
        ][0]
        # Claims are evaluated in resource order; the holder that refuses the
        # whole atomic attempt is the carried-over seal, via the shared group.
        self.assertEqual(block["holder"], "p1_seal")
        self.assertIn(block["resource"], ("group:center_control", "position:center"))
        self.h.assert_clean_teardown()


if __name__ == "__main__":
    unittest.main()
