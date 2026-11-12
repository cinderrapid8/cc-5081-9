"""Mutation tests proving the acceptance suite actually fails on defects.

A property suite that never goes red cannot be trusted.  These tests inject
two controlled, well-scoped faults through the runner's fault switches -- the
production coordinator source is never touched -- and require the suite to
detect each one, produce a replay, and then pass again once the switch is off.

Faults:

* ``double_dispatch`` -- one mechanic start is counted twice, simulating a
  duplicate dispatch;
* ``skip_cleanup`` -- a claim is re-seized after teardown, simulating an
  overlooked reset cleanup.
"""

import json
import os
import tempfile
import unittest

from encounter.acceptance.generator import generate_scenarios
from encounter.acceptance.replay import ReplayWriter, load_replay
from encounter.acceptance.runner import AcceptanceRunner


class MutationDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenarios = generate_scenarios()

    def test_healthy_suite_passes(self) -> None:
        for scenario in self.scenarios:
            result = AcceptanceRunner().check(scenario)
            self.assertTrue(result.passed, scenario.scenario_id)

    def test_duplicate_dispatch_is_detected(self) -> None:
        detected = 0
        example = None
        for scenario in self.scenarios:
            result = AcceptanceRunner(double_dispatch=True).check(scenario)
            if not result.passed:
                detected += 1
                if example is None:
                    example = (scenario, result)
        self.assertGreater(detected, 0, "double-dispatch defect went undetected")
        scenario, result = example
        failure = result.first_failure
        self.assertIsNotNone(failure)
        start_diffs = [
            diff for diff in failure.counter_diffs if diff["counter"] == "mechanic_start"
        ]
        self.assertTrue(start_diffs, "duplicate dispatch did not alter start counts")

    def test_skipped_cleanup_is_detected_and_classified_as_reset(self) -> None:
        detected = 0
        reset_violations = 0
        example = None
        for scenario in self.scenarios:
            result = AcceptanceRunner(skip_cleanup=True).check(scenario)
            if not result.passed:
                detected += 1
                reset_violations += sum(
                    1 for v in result.property_violations if v["property"] == "reset"
                )
                if example is None:
                    example = (scenario, result)
        self.assertGreater(detected, 0, "cleanup-skip defect went undetected")
        self.assertGreater(reset_violations, 0)
        scenario, result = example
        # The leaked claim is visible as an occupancy divergence, not just text.
        failure = result.first_failure
        occupancy_diffs = [
            diff for diff in failure.snapshot_diffs if diff["field"] == "occupancy"
        ]
        self.assertTrue(occupancy_diffs)

    def test_mutation_failures_write_openable_replays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            writer = ReplayWriter(tmp)
            scenario = next(
                s for s in self.scenarios
                if s.scenario_id == "edge_lethal_damage_skips_straight_to_victory"
            )
            result = AcceptanceRunner(skip_cleanup=True).check(scenario)
            self.assertFalse(result.passed)
            path = writer.write_failure(scenario, result, mutation="skip_cleanup")
            self.assertTrue(os.path.exists(path))
            document = load_replay(path)
            self.assertEqual(document["schema"], "encounter-replay/1")
            self.assertFalse(document["passed"])
            self.assertEqual(document["mutation"], "skip_cleanup")
            # The replay keeps the full action sequence and per-step snapshots.
            self.assertTrue(document["steps"])
            self.assertIn("expected_snapshot", document["steps"][0])
            self.assertIn("actual_snapshot", document["steps"][0])
            # Property attribution is recorded so failures are locatable.
            properties = {f["property"] for f in document["property_failures"]}
            self.assertIn("reset", properties)

    def test_replay_round_trips_through_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            writer = ReplayWriter(tmp)
            scenario = next(
                s for s in self.scenarios
                if s.scenario_id == "edge_overlapping_boundary_priority_and_block"
            )
            result = AcceptanceRunner(double_dispatch=True).check(scenario)
            path = writer.write_failure(scenario, result, mutation="double_dispatch")
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
            # The embedded scenario is directly replayable by the client.
            from encounter.acceptance.actions import action_to_dict

            self.assertEqual(
                document["scenario"]["actions"],
                [action_to_dict(a) for a in scenario.actions],
            )

    def test_normal_implementation_passes_after_mutants_removed(self) -> None:
        # Explicit counterpart: once both fault switches are off the exact same
        # scenarios that detected the mutants are green again.
        mutant_scenarios = [
            "edge_lethal_damage_skips_straight_to_victory",
            "edge_overlapping_boundary_priority_and_block",
        ]
        for scenario_id in mutant_scenarios:
            scenario = next(s for s in self.scenarios if s.scenario_id == scenario_id)
            self.assertTrue(AcceptanceRunner().check(scenario).passed)


if __name__ == "__main__":
    unittest.main()
