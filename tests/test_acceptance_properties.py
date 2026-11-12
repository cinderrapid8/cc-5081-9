"""Differential property tests: independent oracle vs. the real coordinator.

These tests never assert on win/lose text.  Every generated scenario is run on
both the production coordinator and the independently written
:class:`ReferenceCoordinator`, and each step is compared on:

* the phase trajectory (phase id/index and full eligibility state);
* the occupancy set (resource/mutex claims and their holders);
* the actual dispatch counts (starts, ends, blocks, revokes, defers, rejected
  interrupts and stale-timer drops).

The handcrafted contracts cover the dangerous corners the enumeration may miss;
the bounded enumeration covers breadth.
"""

import unittest

from encounter.acceptance.generator import (
    FEATURES,
    MAX_DEPTH,
    MAX_NODES,
    enumerate_scenarios,
    generate_scenarios,
    handcrafted_scenarios,
)
from encounter.acceptance.runner import AcceptanceRunner


class DifferentialPropertyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenarios = generate_scenarios()

    def test_enumeration_is_deterministic_without_random_seeds(self) -> None:
        # Two independent generations must be identical: the suite relies on
        # exhaustive bounded search, not a lucky seeded sample.
        first = [s.actions for s in generate_scenarios()]
        second = [s.actions for s in generate_scenarios()]
        self.assertEqual(first, second)

    def test_enumeration_respects_its_bounds(self) -> None:
        scenarios = enumerate_scenarios(max_depth=MAX_DEPTH, max_nodes=MAX_NODES)
        self.assertGreater(len(scenarios), 0)
        for scenario in scenarios:
            self.assertLessEqual(len(scenario.actions), MAX_DEPTH)

    def test_every_scenario_matches_the_reference_model(self) -> None:
        mismatches = []
        for scenario in self.scenarios:
            result = AcceptanceRunner().check(scenario)
            if not result.passed:
                failure = result.first_failure
                mismatches.append(
                    {
                        "scenario": scenario.scenario_id,
                        "reason": result.reason,
                        "snapshot": failure.snapshot_diffs if failure else None,
                        "counters": failure.counter_diffs if failure else None,
                    }
                )
        self.assertEqual(mismatches, [], f"oracle divergences: {mismatches[:3]}")

    def test_handcrafted_corner_cases_are_all_present(self) -> None:
        ids = {scenario.scenario_id for scenario in handcrafted_scenarios()}
        expected = {
            "edge_big_damage_crosses_all_thresholds",
            "edge_cancel_and_natural_completion_same_tick",
            "edge_two_wipes_and_three_rounds",
            "edge_stale_callback_from_wiped_round_rejected",
        }
        self.assertTrue(expected.issubset(ids))

    def test_big_damage_crosses_several_thresholds_stepwise(self) -> None:
        scenario = next(
            s for s in self.scenarios
            if s.scenario_id == "edge_big_damage_crosses_all_thresholds"
        )
        result = AcceptanceRunner().check(scenario)
        self.assertTrue(result.passed, result.reason)
        # One 560 hit dispatched the high-boundary mechanics and deferred the
        # P2-owned one rather than starting it out of phase.
        self.assertEqual(result.actual_counters["mechanic_start"].get("p1_seal"), 1)
        self.assertEqual(result.actual_counters["mechanic_start"].get("p1_last_wrath"), 1)
        self.assertEqual(result.actual_counters["mechanic_blocked"].get("p1_surge"), 1)
        self.assertEqual(result.actual_counters["mechanic_deferred"].get("p2_burn"), 1)

    def test_cancel_and_natural_completion_arrive_in_one_pump(self) -> None:
        scenario = next(
            s for s in self.scenarios
            if s.scenario_id == "edge_cancel_and_natural_completion_same_tick"
        )
        result = AcceptanceRunner().check(scenario)
        self.assertTrue(result.passed, result.reason)
        # The channel ended exactly once (natural completion); the interrupt
        # landing afterwards was rejected, not counted as a second end.
        self.assertEqual(result.actual_counters["mechanic_end"].get("p1_channel"), 1)
        self.assertEqual(
            result.actual_counters["interrupt_rejected"].get("p1_channel"), 1
        )
        # The phase-change cancel still landed in the same run.
        self.assertGreaterEqual(
            sum(result.actual_counters["mechanic_end"].values()), 2
        )

    def test_multiple_wipe_restart_rounds_are_independent(self) -> None:
        scenario = next(
            s for s in self.scenarios
            if s.scenario_id == "edge_two_wipes_and_three_rounds"
        )
        result = AcceptanceRunner().check(scenario)
        self.assertTrue(result.passed, result.reason)
        # The third generation starts clean and wins; two full restarts mean
        # the 80% seal dispatched once per round, never leaking between them.
        self.assertEqual(result.actual_counters["mechanic_start"].get("p1_seal"), 2)
        last = result.steps[-1].actual_snapshot
        self.assertEqual(last["status"], "victory")
        self.assertEqual(last["generation"], 3)
        self.assertEqual(last["occupancy"], [])

    def test_stale_callback_is_rejected_with_evidence(self) -> None:
        scenario = next(
            s for s in self.scenarios
            if s.scenario_id == "edge_stale_callback_from_wiped_round_rejected"
        )
        result = AcceptanceRunner().check(scenario)
        self.assertTrue(result.passed, result.reason)
        self.assertEqual(result.actual_counters["timer_dropped_stale"], 1)
        # The stale gen-1 timer never started a second channel: only the gen-2
        # round's own channel exists.
        self.assertEqual(result.actual_counters["mechanic_start"].get("p1_channel"), 1)
        # The raw event stream carries the rejection evidence.
        stale_events = [
            event
            for step in result.steps
            for event in step.events
            if event["type"] == "timer_dropped_stale"
        ]
        self.assertEqual(len(stale_events), 1)
        payload = stale_events[0]["payload"]
        self.assertEqual(payload["timer_generation"], 1)
        self.assertEqual(payload["current_generation"], 2)

    def test_every_named_feature_is_exercised_at_least_once(self) -> None:
        covered = {feature for scenario in self.scenarios for feature in scenario.features}
        missing = set(FEATURES) - covered
        self.assertEqual(missing, set(), f"features never exercised: {missing}")

    def test_reference_model_is_independent_of_production_coordinator(self) -> None:
        import inspect

        from encounter.acceptance import oracle as oracle_module

        source = inspect.getsource(oracle_module)
        # The oracle must reason from config itself, not borrow the production
        # coordinator or its resource manager.
        self.assertNotIn("from ..coordinator", source)
        self.assertNotIn("from ..resources", source)
        self.assertNotIn("import coordinator", source)


if __name__ == "__main__":
    unittest.main()
