"""Coverage ledger tests: bounded enumeration plus honest uncovered states.

The suite must not pretend a finite search explored the whole state space.
These tests pin the enumeration bounds, require every named feature to be hit,
and require the ledger to enumerate the feasible (state x feature) combinations
it did *not* reach.
"""

import unittest

from encounter.acceptance.generator import (
    FEASIBLE_RULES,
    FEATURES,
    MAX_DEPTH,
    MAX_NODES,
    coverage_ledger,
    enumerate_scenarios,
    generate_scenarios,
)


class CoverageLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenarios = generate_scenarios()
        cls.ledger = coverage_ledger(cls.scenarios).to_dict()

    def test_enumeration_is_bounded(self) -> None:
        # A hard cap keeps the run small and deterministic.
        self.assertLessEqual(len(enumerate_scenarios()), MAX_NODES)
        for scenario in self.scenarios:
            if scenario.origin == "enumerated":
                self.assertLessEqual(len(scenario.actions), MAX_DEPTH)

    def test_every_feature_has_a_hit_and_a_scenario(self) -> None:
        for feature in FEATURES:
            entry = self.ledger["features"][feature]
            self.assertTrue(
                entry["covered"], f"feature {feature} never covered"
            )
            self.assertGreater(entry["hits"], 0)
            self.assertTrue(entry["scenarios"])

    def test_ledger_records_uncovered_feasible_combinations(self) -> None:
        combos = self.ledger["state_combinations"]
        self.assertGreater(combos["possible"], 0)
        self.assertEqual(
            combos["uncovered_count"],
            combos["possible"] - combos["exercised"],
        )
        self.assertEqual(
            len(combos["uncovered"]), combos["uncovered_count"]
        )
        # The honest statement: finite enumeration leaves feasible states
        # unexplored, and each is spelled out rather than hidden.
        self.assertGreater(combos["uncovered_count"], 0)
        for row in combos["uncovered"][:5]:
            self.assertEqual(len(row), 6)  # phase, hp, occupancy, players, round, feature

    def test_feasible_space_excludes_impossible_combinations(self) -> None:
        # A terminal round can never hold occupancy or living players; such a
        # combination must not be counted as a coverage gap.
        terminal_rules = [rule for rule in FEASIBLE_RULES if rule[0] == "terminal"]
        self.assertEqual(len(terminal_rules), 1)
        _phase, hps, occupancies, players, _rounds = terminal_rules[0]
        self.assertEqual(occupancies, ("empty",))
        self.assertEqual(players, ("none",))
        self.assertEqual(hps, ("zero",))

    def test_bounds_are_recorded_for_reproducibility(self) -> None:
        self.assertEqual(
            self.ledger["enumeration_bounds"],
            {"max_depth": MAX_DEPTH, "max_nodes": MAX_NODES},
        )


if __name__ == "__main__":
    unittest.main()
