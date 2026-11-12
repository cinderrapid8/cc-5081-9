"""JSON configuration loading and validation."""

import json
import os
import tempfile
import unittest

from encounter.config import ConfigLoader
from encounter.scenario import build_encounter_config


def config_dict() -> dict:
    return {
        "encounter_id": "unit",
        "name": "Unit Encounter",
        "boss": {"boss_id": "boss-1", "name": "Boss", "max_hp": 500},
        "player_slots": 2,
        "targets": [
            {"target_id": "orb", "name": "Orb", "max_hp": 50},
        ],
        "phases": [
            {
                "phase_id": "a",
                "name": "A",
                "entry": [],
                "mechanics": [
                    {
                        "mechanic_id": "a_hit",
                        "name": "Hit",
                        "trigger": "hp_threshold",
                        "value": 50,
                        "duration": 4,
                        "resources": ["object:orb"],
                        "mutex_group": "orb-control",
                        "cross_phase": "continue",
                        "priority": 5,
                    }
                ],
            },
            {
                "phase_id": "b",
                "name": "B",
                "entry": [
                    {"kind": "target_state", "ref": "orb", "state": "destroyed"},
                    {"kind": "hp_below", "ref": 50},
                ],
                "mechanics": [
                    {
                        "mechanic_id": "b_tick",
                        "name": "Tick",
                        "trigger": "timer",
                        "value": 3,
                        "repeat_interval": 3,
                        "resources": ["position:z"],
                    }
                ],
            },
        ],
    }


class ConfigTests(unittest.TestCase):
    def test_round_trip_through_json_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "encounter.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(config_dict(), handle)
            config = ConfigLoader.load_file(path)

        self.assertEqual(config.encounter_id, "unit")
        self.assertEqual(config.player_slots, 2)
        mechanic = config.mechanic("a_hit")
        self.assertEqual(mechanic.cross_phase.value, "continue")
        self.assertEqual(mechanic.mutex_group, "orb-control")
        self.assertTrue(config.mechanic("b_tick").is_periodic)
        self.assertEqual(len(config.phases[1].entry), 2)

    def test_shipped_scenario_is_self_consistent(self) -> None:
        config = build_encounter_config()
        config.validate()
        ids = [m.mechanic_id for p in config.phases for m in p.mechanics]
        self.assertEqual(len(ids), len(set(ids)))

    def test_first_phase_with_entry_conditions_is_rejected(self) -> None:
        data = config_dict()
        data["phases"][0]["entry"] = [{"kind": "hp_below", "ref": 90}]
        with self.assertRaises(ValueError):
            ConfigLoader.load(data)

    def test_unknown_required_target_is_rejected(self) -> None:
        data = config_dict()
        data["phases"][0]["mechanics"][0]["required_targets"] = ["ghost"]
        with self.assertRaises(ValueError):
            ConfigLoader.load(data)

    def test_duplicate_mechanic_id_is_rejected(self) -> None:
        data = config_dict()
        data["phases"][1]["mechanics"][0]["mechanic_id"] = "a_hit"
        with self.assertRaises(ValueError):
            ConfigLoader.load(data)

    def test_invalid_enum_value_is_rejected(self) -> None:
        data = config_dict()
        data["phases"][0]["mechanics"][0]["cross_phase"] = "maybe"
        with self.assertRaises(ValueError):
            ConfigLoader.load(data)

    def test_shipped_json_config_drives_a_real_encounter(self) -> None:
        import os

        from encounter.clock import Clock
        from encounter.coordinator import PhaseCoordinator
        from encounter.events import EventBus

        path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "encounter",
            "configs",
            "overlord_rehearsal.json",
        )
        config = ConfigLoader.load_file(path)
        bus = EventBus()
        clock = Clock()
        coordinator = PhaseCoordinator(config, bus, clock)
        coordinator.start_encounter()

        coordinator.apply_damage("boss-overlord", 200, "player-2")  # 80% overlap
        coordinator.apply_damage("boss-overlord", 250, "player-2")  # 550
        coordinator.destroy_target("wardstone", "player-1")         # enter P2
        coordinator.apply_damage("boss-overlord", 260, "player-2")  # 290 -> P3

        self.assertEqual(coordinator.status, "victory")
        self.assertTrue(coordinator.resources.is_empty())
        # The blocked overlap mechanic proves contention ran from JSON data.
        blocked = coordinator.mechanics["p1_surge"].status.value
        self.assertEqual(blocked, "blocked")


if __name__ == "__main__":
    unittest.main()
