"""Graphical client tests and the boss-encounter regression workflow.

The client must be a view onto the real coordinator, never a source of canned
phase/occupancy data.  These tests drive :class:`ClientSession` directly (the
same object the HTTP handlers call) and assert that the rendered phase,
occupancy and mechanic positions change only in response to real coordinator
actions.  They also open an acceptance scenario through the client and run the
manual trigger -> wipe -> reset regression against live coordinator state.
"""

import json
import os
import tempfile
import unittest

from encounter.acceptance.actions import Scenario
from encounter.acceptance.replay import ReplayWriter
from encounter.client.server import ClientSession, create_server, list_acceptance_files
from urllib.request import Request, urlopen
from urllib.error import HTTPError


class ClientSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = ClientSession()

    def test_initial_state_comes_from_coordinator_not_fixed_data(self) -> None:
        state = self.session.state()
        self.assertEqual(state["status"], "idle")
        self.assertIsNone(state["phase"])
        self.assertEqual(state["boss"]["hp"], 1000.0)
        self.assertEqual(state["occupancy"], [])

    def test_button_action_drives_real_phase_and_occupancy(self) -> None:
        self.session.act(("start",))
        state = self.session.act(("boss_damage", 200, "player-2"))
        self.assertEqual(state["phase"]["phase_id"], "p1")
        holders = {c["resource"]: c["owner"] for c in state["occupancy"]}
        self.assertEqual(holders["object:brazier"], "p1_seal")
        self.assertEqual(holders["group:center_control"], "p1_seal")
        # The blocked overlap mechanic is reported with its real status.
        statuses = {m["mechanic_id"]: m["status"] for m in state["mechanic_records"]}
        self.assertEqual(statuses["p1_surge"], "blocked")

    def test_manual_mechanic_then_wipe_reset_regression(self) -> None:
        """The boss-encounter regression: trigger mechanics, wipe, then reset.

        After the reset the client must show the boss back at full HP, the
        phase cleared and zero occupancy -- all read from the coordinator.
        """

        self.session.act(("start",))
        self.session.act(("boss_damage", 200, "player-2"))   # seal + blocked surge
        self.session.act(("wait", 5.0))                       # channel commits
        active = {m["mechanic_id"] for m in self.session.state()["active_mechanics"]}
        self.assertIn("p1_channel", active)
        self.assertIn("p1_seal", active)

        for player in ("player-1", "player-2", "player-3"):
            self.session.act(("kill", player))
        wiped = self.session.state()
        self.assertEqual(wiped["status"], "wipe")
        self.assertEqual(wiped["occupancy"], [])
        self.assertEqual(wiped["active_mechanics"], [])

        reset = self.session.reset()
        self.assertEqual(reset["status"], "idle")
        self.assertEqual(reset["boss"]["hp"], 1000.0)
        self.assertIsNone(reset["phase"])
        self.assertEqual(reset["occupancy"], [])

        # Restarting enters phase one on a fresh generation, still no fixed data.
        restarted = self.session.act(("start",))
        self.assertEqual(restarted["phase"]["phase_id"], "p1")
        self.assertEqual(restarted["generation"], 2)

    def test_client_opens_acceptance_scenario_and_steps_through_it(self) -> None:
        scenario = Scenario(
            "client_open_smoke",
            "opened from the battle-drill client",
            [
                ("start",),
                ("boss_damage", 200, "player-2"),
                ("destroy", "wardstone", "player-1"),
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "client_open_smoke.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(scenario.to_dict(), handle)

            state = self.session.open_scenario(path)
            self.assertEqual(state["mode"], "replay")
            self.assertEqual(state["replay_length"], 3)
            self.session.replay_step()  # start
            state = self.session.replay_step()  # boss damage
            self.assertEqual(state["replay_cursor"], 2)
            holders = {c["resource"]: c["owner"] for c in state["occupancy"]}
            self.assertEqual(holders.get("object:brazier"), "p1_seal")
            final = self.session.replay_all()
            self.assertEqual(final["replay_cursor"], 3)

    def test_failure_replay_is_listed_and_openable(self) -> None:
        scenario = Scenario("listed_failure", "x", [("start",)])
        with tempfile.TemporaryDirectory() as tmp:
            writer = ReplayWriter(tmp)
            from encounter.acceptance.runner import AcceptanceRunner

            result = AcceptanceRunner(double_dispatch=True).check(scenario)
            writer.write_failure(scenario, result, mutation="double_dispatch")
            listing = list_acceptance_files(tmp)
            self.assertTrue(
                any("listed_failure" in name for name in listing["failures"])
            )
            path = os.path.join(tmp, "failures", listing["failures"][0])
            state = self.session.open_scenario(path)
            self.assertEqual(state["mode"], "replay")

    def test_server_serves_live_state_and_rejects_traversal(self) -> None:
        httpd, session = create_server(host="127.0.0.1", port=0)
        port = httpd.server_address[1]
        thread = __import__("threading").Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["encounter_id"], "overlord-rehearsal")

            req = Request(
                f"http://127.0.0.1:{port}/api/replay/open",
                data=json.dumps({"file": "../../etc/passwd"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as ctx:
                urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 400)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
