"""Phase entry gating and explicit cross-phase policies."""

import dataclasses
import unittest

from harness import Harness


class PhaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()
        self.h.start()

    def test_hp_alone_does_not_enter_target_gated_phase(self) -> None:
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")  # 550
        self.assertEqual(self.h.coordinator.current_phase.phase_id, "p1")
        self.h.coordinator.destroy_target("wardstone", "player-1")
        self.assertEqual(self.h.coordinator.current_phase.phase_id, "p2")

    def test_target_destruction_without_hp_does_not_enter_phase(self) -> None:
        self.h.coordinator.destroy_target("wardstone", "player-1")
        self.assertEqual(self.h.coordinator.current_phase.phase_id, "p1")

    def test_elapsed_condition_blocks_until_clock_passes(self) -> None:
        # Build a custom config whose second phase is purely timer-gated.
        from encounter.config import (
            ConditionType,
            PhaseCondition,
            PhaseConfig,
        )

        h = Harness()
        phases = list(h.config.phases)
        gated = PhaseConfig(
            phase_id="timer_gate",
            name="Timer Gate",
            entry=[PhaseCondition(kind=ConditionType.ELAPSED, ref=10)],
            mechanics=[],
        )
        h.config = dataclasses.replace(h.config, phases=[phases[0], gated])
        h.coordinator.config = h.config
        h.start()
        h.coordinator.tick(9.99)
        self.assertEqual(h.coordinator.current_phase.phase_id, "p1")
        h.coordinator.tick(10.0)
        self.assertEqual(h.coordinator.current_phase.phase_id, "timer_gate")

    def test_cross_phase_continue_keeps_claims_and_emits_event(self) -> None:
        self.h.coordinator.apply_damage("boss-overlord", 200, "player-2")  # seal starts
        self.h.coordinator.apply_damage("boss-overlord", 250, "player-2")  # 550
        self.h.coordinator.destroy_target("wardstone", "player-1")         # P2

        carried = self.h.ids("mechanic_carried_over")
        self.assertEqual(carried, ["p1_seal"])
        seal = self.h.coordinator.mechanics["p1_seal"]
        self.assertTrue(seal.active)
        self.assertEqual(
            self.h.coordinator.resources.holder_of("position:center"), "p1_seal"
        )

    def test_cross_phase_cancel_releases_everything(self) -> None:
        # p1_pulse is CANCEL and runs at t=8.
        self.h.coordinator.tick(8.0)
        self.assertEqual(
            self.h.coordinator.resources.holder_of("position:west_ring"), "p1_pulse"
        )
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")
        self.h.coordinator.destroy_target("wardstone", "player-1")  # enter P2

        pulse = self.h.coordinator.mechanics["p1_pulse"]
        self.assertEqual(pulse.status.value, "cancelled")
        self.assertEqual(pulse.detail, "phase_change")
        self.assertIsNone(
            self.h.coordinator.resources.holder_of("position:west_ring")
        )

    def test_unstarted_timer_mechanic_is_revoked_on_phase_change(self) -> None:
        self.h.coordinator.apply_damage("boss-overlord", 450, "player-2")
        # Before t=5 the channel never started; entering P2 must revoke it.
        self.h.coordinator.destroy_target("wardstone", "player-1")
        revoked = self.h.ids("mechanic_revoked")
        self.assertIn("p1_channel", revoked)
        self.assertIn("p1_pulse", revoked)
        instance = self.h.coordinator.mechanics["p1_channel"]
        self.assertEqual(instance.detail, "phase_change")

    def ids(self, event_type: str) -> list[str]:
        return [p["mechanic_id"] for p in self.h.payloads(event_type)]


if __name__ == "__main__":
    unittest.main()
